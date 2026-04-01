# Feature: SMT Divergence Strategy on MNQ1!

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Add a second, independent trading strategy to auto-co-trader that trades **MNQ1! (Micro E-mini NASDAQ 100 futures)** long or short using ICT Smart Money Concepts:

- **SMT Divergence**: MES (S&P 500 micro futures) vs MNQ (NASDAQ micro futures) divergence at session swing highs/lows detects institutional liquidity sweeps and establishes directional bias
- **Kill Zone**: 1-minute inner loop from 9:00 AM–10:30 AM ET (configurable) focuses on the NY open session where institutional activity is highest
- **TDO Anchor**: True Day Open (9:30 AM bar open) is both the take-profit target and the basis for stop placement

The strategy is fully isolated in a new `train_smt.py` file and does not touch the existing equity strategy (`train.py`). A new `prepare_futures.py` downloads MNQ/MES 1m bars via IB-Gateway.

## User Story

As a trader using auto-co-trader,
I want to backtest and optimize an SMT divergence strategy on MNQ futures,
So that I can evaluate and tune a second, independent signal source separate from the equity momentum strategy.

## Problem Statement

The existing strategy only trades equities long on daily bars. ICT-based intraday futures strategies operate on a fundamentally different logic (1m bars, two-instrument divergence, fixed R:R, intraday session exits) that cannot be expressed in the current harness without breaking it.

## Solution Statement

Create a parallel `train_smt.py` with its own 1m intraday backtesting loop that shares only the data layer (`data/sources.py`) with the existing system. Extend `IBGatewaySource` to support `ContFuture` contracts. Add `prepare_futures.py` for futures-specific data download. Write `program_smt.md` to direct the optimization agent.

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: High
**Primary Systems Affected**: `data/sources.py`, new `train_smt.py`, new `prepare_futures.py`
**Dependencies**: `ib_insync>=0.9` (already in pyproject.toml), IB-Gateway running
**Breaking Changes**: No — existing `train.py`, `prepare.py`, `screener.py` untouched

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `data/sources.py` (lines 101–194) — IBGatewaySource to extend with `contract_type`; pattern for `ContFuture`
- `data/sources.py` (lines 62–86) — `_IB_BAR_SIZE` and `_IB_CHUNK_DAYS` dicts — reuse for futures
- `data/sources.py` (lines 89–98) — `_to_et()` helper — reuse in prepare_futures.py
- `prepare.py` (lines 1–120) — Config pattern, env vars, `CACHE_DIR`, `MAX_WORKERS`, manifest writing — mirror for prepare_futures.py
- `prepare.py` (lines 117–200) — `resample_to_daily()` — NOT needed for futures (store raw 1m)
- `train.py` (lines 1–110) — Manifest loading pattern (`_load_manifest`, `_FETCH_INTERVAL`) — mirror for train_smt.py
- `train.py` (lines 472–843) — `run_backtest()` structure — adapt for intraday loop
- `train.py` (lines 945–1044) — Fold loop and print block — mirror exactly
- `tests/conftest.py` (lines 1–160) — Test fixture pattern; `pytest_configure` manifest bootstrap — add futures manifest bootstrap
- `tests/test_data_sources.py` — Pattern for mocking `ib_insync` in tests
- `test_ib_connection.py` (root) — Demonstrates `ContFuture` and `reqHistoricalData` for MES/MNQ

### New Files to Create

- `prepare_futures.py` — Download MNQ + MES 1m bars from IB-Gateway; write `futures_manifest.json`
- `train_smt.py` — SMT strategy functions + 1m intraday backtest harness
- `program_smt.md` — Optimization agent instructions (mirrors `program.md` for SMT strategy)
- `tests/test_smt_strategy.py` — Unit tests for all strategy functions
- `tests/test_smt_backtest.py` — Integration tests for the backtest harness

### Patterns to Follow

**Naming Conventions**: snake_case functions, UPPER_SNAKE constants, `_private` helpers
**Error Handling**: return `None` on data failure (never raise); `print(f"  {Class}: error...")` for IB errors (matches existing pattern)
**Logging**: No logging framework — print only for user-visible progress in prepare scripts
**Manifest**: Always write `futures_manifest.json` after data download; `train_smt.py` reads it at module level via `_load_futures_manifest()`
**Tests**: `uv run pytest tests/` — session fixture in conftest.py; use `pytest.mark.skip` not `pytest.importorskip` for optional network

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 1: Data Foundation (Parallel)                               │
├──────────────────────────────────────────────────────────────────┤
│ Task 1.1: ADD ContFuture support    │ Task 1.2: CREATE           │
│ to data/sources.py                 │ prepare_futures.py          │
│ Agent: data-engineer               │ Agent: data-engineer        │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 2: Strategy Core (Parallel)                                 │
├──────────────────────────────────────────────────────────────────┤
│ Task 2.1: CREATE strategy           │ Task 2.2: CREATE           │
│ functions in train_smt.py          │ tests/test_smt_strategy.py  │
│ (editable section, lines 1–N)      │ Agent: test-engineer        │
│ Agent: strategy-engineer           │                             │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 3: Harness (Sequential — needs Wave 2)                      │
├──────────────────────────────────────────────────────────────────┤
│ Task 3.1: CREATE harness in train_smt.py (frozen section)        │
│ Agent: harness-engineer                                          │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 4: Integration (Parallel)                                   │
├──────────────────────────────────────────────────────────────────┤
│ Task 4.1: CREATE                    │ Task 4.2: UPDATE           │
│ tests/test_smt_backtest.py         │ conftest.py + CREATE        │
│ Agent: test-engineer               │ program_smt.md              │
│                                    │ Agent: integration-engineer  │
└──────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 and 1.2 — no dependencies
**Wave 2 — Parallel after Wave 1**: Tasks 2.1 and 2.2 — both need data/sources.py ContFuture
**Wave 3 — Sequential**: Task 3.1 — needs strategy functions from 2.1
**Wave 4 — Parallel after Wave 3**: Tasks 4.1 and 4.2

### Interface Contracts

**Contract 1**: Task 1.1 provides `IBGatewaySource.fetch(..., contract_type="contfuture")` → Task 1.2 consumes in `prepare_futures.py`
**Contract 2**: Task 2.1 provides `screen_session()`, `manage_position()`, `detect_smt_divergence()` → Task 3.1 consumes in harness
**Contract 3**: Task 3.1 provides `run_backtest(mnq_df, mes_df, start, end)` → Task 4.1 consumes in integration tests

### Synchronization Checkpoints

**After Wave 1**: `uv run python -c "from data.sources import IBGatewaySource; print('ok')"` (import check only — no IB connection needed)
**After Wave 2**: `uv run pytest tests/test_smt_strategy.py -x`
**After Wave 3**: `uv run pytest tests/test_smt_backtest.py -x`
**After Wave 4**: `uv run pytest tests/ -x`

---

## IMPLEMENTATION PLAN

### Phase 1: Data Layer Extension

#### Task 1.1: ADD ContFuture support to `data/sources.py`

**Purpose**: Allow `IBGatewaySource` to fetch continuous futures contracts (MNQ, MES) in addition to stocks.

**Steps**:
1. Add optional `contract_type: str = "stock"` parameter to `IBGatewaySource.fetch()`
2. Import `ContFuture` from `ib_insync` alongside `Stock`
3. Branch on `contract_type`:
   - `"stock"` → `Stock(ticker, "SMART", "USD")` (unchanged behavior)
   - `"contfuture"` → `ContFuture(ticker, "CME", "USD")`
4. For `contfuture`, set `useRTH=False` — futures RTH excludes the 9:00–9:30 ET window needed for this strategy; full Globex session is required
5. Keep `useRTH=True` for stocks (existing behavior)

**Implementation**:
```python
def fetch(
    self,
    ticker: str,
    start: str,
    end: str,
    interval: str = "1h",
    contract_type: str = "stock",
) -> pd.DataFrame | None:
    from ib_insync import IB, Stock, ContFuture, util
    ...
    if contract_type == "contfuture":
        contract = ContFuture(ticker, "CME", "USD")
        use_rth = False
    else:
        contract = Stock(ticker, "SMART", "USD")
        use_rth = True
    ib.qualifyContracts(contract)
    ...
    bars = ib.reqHistoricalData(
        contract,
        ...
        useRTH=use_rth,
        ...
    )
```

**Note**: `contract_type` is not in the `DataSource` Protocol signature. This is intentional — the Protocol defines the minimum interface; `IBGatewaySource` may accept additional keyword args. Callers that need futures must use `IBGatewaySource` directly (not via the Protocol).

**Validation**: `uv run python -c "from data.sources import IBGatewaySource; src = IBGatewaySource(); print(src.fetch.__doc__)"`

---

#### Task 1.2: CREATE `prepare_futures.py`

**Purpose**: Download MNQ and MES 1-minute bars from IB-Gateway and cache them for `train_smt.py`.

**Pattern**: Mirror `prepare.py` structure — env vars, `process_ticker()`, `ThreadPoolExecutor`, manifest.

**Key differences from prepare.py**:
- No `resample_to_daily()` — store raw 1m bars
- Only 2 tickers: MNQ and MES
- Cache dir: `~/.cache/autoresearch/futures_data/` (separate from equity cache)
- Manifest file: `futures_manifest.json`
- Always uses `IBGatewaySource` with `contract_type="contfuture"`
- No `PREPARE_SOURCE` env var (always IB)

**Implementation outline**:
```python
"""prepare_futures.py — Download MNQ/MES 1m futures bars from IB-Gateway.

Usage:
    uv run prepare_futures.py

Requires IB-Gateway running on localhost:4002.
Data cached to ~/.cache/autoresearch/futures_data/1m/.
"""
import datetime, json, os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd
from data.sources import IBGatewaySource

TICKERS = ["MNQ", "MES"]            # IB ContFuture symbols (not TradingView 1! suffix)
BACKTEST_START = "2024-09-01"
BACKTEST_END   = "2026-03-20"
HISTORY_START  = BACKTEST_START      # No warmup needed — no daily SMAs
INTERVAL       = "1m"
CACHE_DIR = os.environ.get(
    "FUTURES_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
)
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4002"))
MAX_WORKERS = 2                       # Only 2 tickers; sequential is fine but keep the pattern


def process_ticker(ticker: str) -> bool:
    """Fetch and cache 1m bars for one futures ticker. Returns True on success."""
    out_path = Path(CACHE_DIR) / INTERVAL / f"{ticker}.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        print(f"  {ticker}: already cached, skipping")
        return True
    source = IBGatewaySource(host=IB_HOST, port=IB_PORT, client_id=2)
    df = source.fetch(ticker, HISTORY_START, BACKTEST_END, INTERVAL, contract_type="contfuture")
    if df is None or df.empty:
        print(f"  {ticker}: no data returned")
        return False
    df.to_parquet(out_path)
    print(f"  {ticker}: saved {len(df)} bars to {out_path}")
    return True


def write_manifest() -> None:
    manifest_path = Path(CACHE_DIR) / "futures_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "tickers": TICKERS,
            "backtest_start": BACKTEST_START,
            "backtest_end": BACKTEST_END,
            "fetch_interval": INTERVAL,
            "source": "ib",
        }, f, indent=2)
    print(f"Manifest written to {manifest_path}")


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(process_ticker, TICKERS))
    if not all(results):
        sys.exit(1)
    write_manifest()
    print("Done.")
```

**Validation**: `uv run python -c "import prepare_futures; print('import ok')"` (import only — no IB needed for import)

---

### Phase 2: Core Strategy Functions

#### Task 2.1: CREATE strategy functions section of `train_smt.py` (editable section, above the harness boundary)

**Purpose**: Implement all signal logic. These functions are the agent's optimization target — they sit above `# DO NOT EDIT BELOW THIS LINE`.

**File structure**: `train_smt.py` follows the exact same two-section pattern as `train.py`:
- Lines 1 → boundary: editable strategy logic
- Below boundary: frozen harness (written in Task 3.1)

**Constants block** (editable):
```python
# ══ SESSION SETUP ═══════════════════════════════════════════════════════
FUTURES_CACHE_DIR = os.environ.get(
    "FUTURES_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
)
BACKTEST_START: str  # loaded from futures_manifest.json
BACKTEST_END:   str  # loaded from futures_manifest.json
TRAIN_END   = "2026-03-06"
TEST_START  = "2026-03-06"
SILENT_END  = "2026-02-20"
WALK_FORWARD_WINDOWS = 6
FOLD_TEST_DAYS       = 60
FOLD_TRAIN_DAYS      = 0
RISK_PER_TRADE       = 50.0   # dollar risk per trade
WRITE_FINAL_OUTPUTS  = False

# ══ STRATEGY TUNING ══════════════════════════════════════════════════════
SESSION_START          = "09:00"   # NY ET — start scanning for SMT
SESSION_END            = "10:30"   # NY ET — force-exit any open position
MIN_BARS_BEFORE_SIGNAL = 5         # minimum bars into session before a signal fires
MNQ_PNL_PER_POINT      = 2.0      # MNQ = $2 per point per contract
```

**Strategy functions** (implement these in the editable section):

```python
def _load_futures_manifest() -> dict:
    """Load futures_manifest.json written by prepare_futures.py."""
    path = Path(FUTURES_CACHE_DIR) / "futures_manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"No futures_manifest.json at {path}. Run prepare_futures.py first.")
    with open(path) as f:
        return json.load(f)


def load_futures_data() -> dict[str, pd.DataFrame]:
    """Load MNQ and MES 1m parquets from FUTURES_CACHE_DIR/1m/."""
    ...


def detect_smt_divergence(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    session_start_idx: int,
) -> str | None:
    """
    Check for SMT divergence at bar_idx.

    Returns "short" if MES makes new session high but MNQ does not.
    Returns "long"  if MES makes new session low  but MNQ does not.
    Returns None if no divergence or not enough bars since session start.

    Args:
        mes_bars: 1m OHLCV DataFrame for MES, index = ET datetime
        mnq_bars: 1m OHLCV DataFrame for MNQ, same index alignment
        bar_idx: current bar position in the session slice
        session_start_idx: first bar index of current session
    """
    if bar_idx - session_start_idx < MIN_BARS_BEFORE_SIGNAL:
        return None

    session_slice = slice(session_start_idx, bar_idx)  # exclude current bar
    mes_session_high = mes_bars["High"].iloc[session_slice].max()
    mes_session_low  = mes_bars["Low"].iloc[session_slice].min()
    mnq_session_high = mnq_bars["High"].iloc[session_slice].max()
    mnq_session_low  = mnq_bars["Low"].iloc[session_slice].min()

    cur_mes = mes_bars.iloc[bar_idx]
    cur_mnq = mnq_bars.iloc[bar_idx]

    if cur_mes["High"] > mes_session_high and cur_mnq["High"] <= mnq_session_high:
        return "short"
    if cur_mes["Low"] < mes_session_low and cur_mnq["Low"] >= mnq_session_low:
        return "long"
    return None


def find_entry_bar(
    mnq_bars: pd.DataFrame,
    direction: str,
    divergence_idx: int,
    session_end_idx: int,
) -> int | None:
    """
    Find the confirmation candle after a divergence signal.

    For "short": first bar after divergence_idx where:
        - close < open  (bearish bar)
        - high > close of most recent prior bullish bar (wick pierces bull body)

    For "long": first bar after divergence_idx where:
        - close > open  (bullish bar)
        - low < close of most recent prior bearish bar (wick pierces bear body)

    Returns bar index or None if no confirmation before session_end_idx.
    """
    # Find last opposite candle before divergence (scan backwards)
    if direction == "short":
        last_opposite_body = None
        for i in range(divergence_idx, -1, -1):
            bar = mnq_bars.iloc[i]
            if bar["Close"] > bar["Open"]:   # bullish
                last_opposite_body = bar["Close"]  # top of bull body
                break
        if last_opposite_body is None:
            return None
        for i in range(divergence_idx + 1, session_end_idx):
            bar = mnq_bars.iloc[i]
            if bar["Close"] < bar["Open"] and bar["High"] > last_opposite_body:
                return i
    else:  # "long"
        last_opposite_body = None
        for i in range(divergence_idx, -1, -1):
            bar = mnq_bars.iloc[i]
            if bar["Close"] < bar["Open"]:   # bearish
                last_opposite_body = bar["Close"]  # bottom of bear body
                break
        if last_opposite_body is None:
            return None
        for i in range(divergence_idx + 1, session_end_idx):
            bar = mnq_bars.iloc[i]
            if bar["Close"] > bar["Open"] and bar["Low"] < last_opposite_body:
                return i
    return None


def compute_tdo(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """
    Return True Day Open = opening price of the 9:30 AM ET bar for given date.
    Returns None if no 9:30 bar found (e.g., holiday/weekend).
    Falls back to 9:00 AM bar open if called before 9:30 AM bars exist.
    """
    target_time = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")
    if target_time in mnq_bars.index:
        return float(mnq_bars.loc[target_time, "Open"])
    # Proxy: first available bar on that date
    day_bars = mnq_bars[mnq_bars.index.date == date]
    if day_bars.empty:
        return None
    return float(day_bars.iloc[0]["Open"])


def screen_session(
    mnq_bars: pd.DataFrame,
    mes_bars: pd.DataFrame,
    date: datetime.date,
) -> dict | None:
    """
    Run the full SMT signal pipeline for one session.

    Slices SESSION_START–SESSION_END bars, scans for first divergence,
    then looks for an entry bar. Returns a signal dict or None.

    Signal dict:
        direction:       "long" | "short"
        entry_price:     float  (close of confirmation bar)
        entry_time:      pd.Timestamp
        take_profit:     float  (TDO)
        stop_price:      float  (entry ± 0.45 × distance_to_TDO)
        tdo:             float
        divergence_bar:  int    (absolute index in session slice)
        entry_bar:       int    (absolute index in session slice)
    """
    ...


def manage_position(
    position: dict,
    current_bar: pd.Series,
) -> str:
    """
    Check exit conditions for an open position against one 1m bar.

    Returns one of: "hold" | "exit_tp" | "exit_stop" | "exit_time"

    For longs:  stop hit if low  <= stop_price; TP hit if high >= take_profit
    For shorts: stop hit if high >= stop_price; TP hit if low  <= take_profit
    Exit time is handled by the harness (not this function).
    """
    direction = position["direction"]
    stop      = position["stop_price"]
    tp        = position["take_profit"]

    if direction == "long":
        if current_bar["Low"]  <= stop: return "exit_stop"
        if current_bar["High"] >= tp:   return "exit_tp"
    else:  # short
        if current_bar["High"] >= stop: return "exit_stop"
        if current_bar["Low"]  <= tp:   return "exit_tp"
    return "hold"
```

**Validation**: `uv run pytest tests/test_smt_strategy.py -x`

---

#### Task 2.2: CREATE `tests/test_smt_strategy.py`

**Purpose**: Unit tests for all strategy functions using synthetic 1m DataFrames (no IB connection needed).

**Helper**: Create `_make_1m_bars(opens, highs, lows, closes, start_time)` to build a synthetic 1m DataFrame with a proper ET DatetimeIndex.

**Test cases** (implement all):

| # | Test name | What it verifies |
|---|-----------|-----------------|
| 1 | `test_detect_smt_bearish` | MES new high + MNQ fails → "short" |
| 2 | `test_detect_smt_bullish` | MES new low + MNQ fails → "long" |
| 3 | `test_detect_smt_both_confirm_none` | Both make new high → None |
| 4 | `test_detect_smt_min_bars_suppresses` | < MIN_BARS_BEFORE_SIGNAL → None |
| 5 | `test_detect_smt_resets_on_opposite` | Bearish then bullish → latest bias wins |
| 6 | `test_find_entry_bar_short` | Bearish bar + upper wick past bull body → returns index |
| 7 | `test_find_entry_bar_long` | Bullish bar + lower wick past bear body → returns index |
| 8 | `test_find_entry_bar_no_match_returns_none` | No confirmation before end → None |
| 9 | `test_find_entry_requires_wick_past_body` | Bar in direction but wick insufficient → None |
| 10 | `test_compute_tdo_finds_930_bar` | 9:30 bar exists → returns its open |
| 11 | `test_compute_tdo_proxy_no_930_bar` | 9:30 bar absent → returns first bar's open |
| 12 | `test_compute_tdo_returns_none_on_empty` | No bars for date → None |
| 13 | `test_stop_short` | entry=20100, TDO=20000 → stop=20145 (entry + 0.45×100) |
| 14 | `test_stop_long` | entry=19900, TDO=20000 → stop=19855 (entry - 0.45×100) |
| 15 | `test_tp_equals_tdo` | TP is always TDO |
| 16 | `test_rr_ratio` | distance_to_TP / distance_to_stop ≈ 2.22 |
| 17 | `test_screen_session_returns_short_signal` | Full pipeline: bearish SMT + confirm → short signal dict |
| 18 | `test_screen_session_returns_long_signal` | Full pipeline: bullish SMT + confirm → long signal dict |
| 19 | `test_screen_session_no_divergence_returns_none` | Flat market → None |
| 20 | `test_manage_position_tp_long` | Long: high >= TP → "exit_tp" |
| 21 | `test_manage_position_stop_long` | Long: low <= stop → "exit_stop" |
| 22 | `test_manage_position_tp_short` | Short: low <= TP → "exit_tp" |
| 23 | `test_manage_position_stop_short` | Short: high >= stop → "exit_stop" |
| 24 | `test_manage_position_hold` | Neither triggered → "hold" |

**Run**: `uv run pytest tests/test_smt_strategy.py -v`

---

### Phase 3: Harness

#### Task 3.1: CREATE harness in `train_smt.py` (frozen section, below boundary)

**Purpose**: Walk-forward backtesting engine that calls strategy functions, tracks position, records trades, computes metrics, and prints results in a format the optimization agent can parse.

**Boundary comment**: `# DO NOT EDIT BELOW THIS LINE` (same pattern as `train.py`)

**Harness structure**:

```python
# ─────────────────────────────────────────────────────────
# DO NOT EDIT BELOW THIS LINE — harness is frozen
# ─────────────────────────────────────────────────────────

def _load_futures_manifest() -> dict:
    ...  # (moved from editable section — also usable here)

def load_futures_data() -> dict[str, pd.DataFrame]:
    """Load MNQ and MES 1m parquets. Returns {"MNQ": df, "MES": df}."""
    manifest = _load_futures_manifest()
    interval = manifest.get("fetch_interval", "1m")
    result = {}
    for ticker in ["MNQ", "MES"]:
        path = Path(FUTURES_CACHE_DIR) / interval / f"{ticker}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Missing futures parquet: {path}. Run prepare_futures.py.")
        result[ticker] = pd.read_parquet(path)
    return result


def run_backtest(
    mnq_df: pd.DataFrame,
    mes_df: pd.DataFrame,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """
    Walk through each trading day in [start, end]. For each day:
      1. Slice SESSION_START–SESSION_END 1m bars
      2. If no open position: call screen_session(); if signal → open position
      3. If open position: call manage_position() bar by bar until exit or SESSION_END
      4. Record each closed trade

    Returns stats dict with all metrics.
    """
    start_dt = pd.Timestamp(start or BACKTEST_START).date()
    end_dt   = pd.Timestamp(end   or BACKTEST_END).date()

    # Get all unique trading days in MNQ data
    trading_days = sorted({
        ts.date() for ts in mnq_df.index
        if start_dt <= ts.date() < end_dt
    })

    position: dict | None = None
    trades: list[dict] = []
    equity_curve: list[float] = [0.0]  # cumulative PnL per day

    for day in trading_days:
        # Slice session window for this day
        session_mask = (
            (mnq_df.index.date == day) &
            (mnq_df.index.time >= pd.Timestamp(f"2000-01-01 {SESSION_START}").time()) &
            (mnq_df.index.time <= pd.Timestamp(f"2000-01-01 {SESSION_END}").time())
        )
        mnq_session = mnq_df[session_mask].copy()
        mes_session = mes_df[session_mask].copy()

        if mnq_session.empty:
            equity_curve.append(equity_curve[-1])
            continue

        day_pnl = 0.0

        if position is None:
            # Screen for new entry
            signal = screen_session(mnq_session, mes_session, day)
            if signal:
                # Size position
                risk_per_contract = abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                contracts = max(1, int(RISK_PER_TRADE / risk_per_contract)) if risk_per_contract > 0 else 1
                position = {
                    "direction":   signal["direction"],
                    "entry_price": signal["entry_price"],
                    "entry_time":  signal["entry_time"],
                    "entry_date":  day,
                    "take_profit": signal["take_profit"],
                    "stop_price":  signal["stop_price"],
                    "tdo":         signal["tdo"],
                    "contracts":   contracts,
                    "divergence_bar": signal["divergence_bar"],
                    "entry_bar":   signal["entry_bar"],
                }
        else:
            # Manage open position bar by bar
            exit_result = None
            exit_bar = None
            for i, (ts, bar) in enumerate(mnq_session.iterrows()):
                result = manage_position(position, bar)
                if result != "hold":
                    exit_result = result
                    exit_bar = bar
                    break

            if exit_result is None:
                # No exit during session — force close at SESSION_END
                exit_result = "session_close"
                exit_bar = mnq_session.iloc[-1]

            # Compute PnL
            direction_sign = 1 if position["direction"] == "long" else -1
            exit_price = (
                position["take_profit"] if exit_result == "exit_tp"
                else position["stop_price"] if exit_result == "exit_stop"
                else float(exit_bar["Close"])
            )
            pnl = direction_sign * (exit_price - position["entry_price"]) * position["contracts"] * MNQ_PNL_PER_POINT

            trades.append({
                "entry_date":    str(position["entry_date"]),
                "entry_time":    str(position["entry_time"].time())[:5] if hasattr(position["entry_time"], "time") else str(position["entry_time"]),
                "exit_time":     str(exit_bar.name.time())[:5] if hasattr(exit_bar.name, "time") else "",
                "direction":     position["direction"],
                "entry_price":   round(position["entry_price"], 4),
                "exit_price":    round(exit_price, 4),
                "tdo":           round(position["tdo"], 4),
                "stop_price":    round(position["stop_price"], 4),
                "contracts":     position["contracts"],
                "pnl":           round(pnl, 2),
                "exit_type":     exit_result,
                "divergence_bar": position["divergence_bar"],
                "entry_bar":     position["entry_bar"],
            })
            day_pnl = pnl
            position = None

        equity_curve.append(equity_curve[-1] + day_pnl)

    # Close any still-open position at end of backtest
    if position is not None:
        last_bar = mnq_df[mnq_df.index.date < end_dt].iloc[-1]
        direction_sign = 1 if position["direction"] == "long" else -1
        exit_price = float(last_bar["Close"])
        pnl = direction_sign * (exit_price - position["entry_price"]) * position["contracts"] * MNQ_PNL_PER_POINT
        trades.append({
            **{k: v for k, v in trades[-1].items() if k not in ("pnl", "exit_type", "exit_price")},
            "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2),
            "exit_type": "end_of_backtest",
        })
        equity_curve.append(equity_curve[-1] + pnl)

    # Compute metrics
    return _compute_metrics(trades, equity_curve)


def _compute_metrics(trades: list[dict], equity_curve: list[float]) -> dict:
    """Compute all performance metrics from trade list and equity curve."""
    total_pnl    = sum(t["pnl"] for t in trades)
    total_trades = len(trades)
    winners      = [t for t in trades if t["pnl"] > 0]
    losers       = [t for t in trades if t["pnl"] <= 0]
    win_rate     = len(winners) / total_trades if total_trades > 0 else 0.0
    avg_pnl      = total_pnl / total_trades if total_trades > 0 else 0.0

    long_pnl  = sum(t["pnl"] for t in trades if t["direction"] == "long")
    short_pnl = sum(t["pnl"] for t in trades if t["direction"] == "short")

    # Sharpe from daily equity changes
    daily_changes = [equity_curve[i] - equity_curve[i-1] for i in range(1, len(equity_curve))]
    if len(daily_changes) > 1:
        import statistics
        mean_chg = sum(daily_changes) / len(daily_changes)
        std_chg  = statistics.stdev(daily_changes) or 1e-9
        sharpe   = (mean_chg / std_chg) * (252 ** 0.5)
    else:
        sharpe = 0.0

    # Max drawdown
    peak, max_dd = 0.0, 0.0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = peak - eq
        if dd > max_dd: max_dd = dd

    calmar = total_pnl / max_dd if max_dd > 0 else 0.0

    exit_types = {}
    for t in trades:
        exit_types[t["exit_type"]] = exit_types.get(t["exit_type"], 0) + 1

    avg_win  = sum(t["pnl"] for t in winners) / len(winners) if winners else 0.0
    avg_loss = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0.0
    avg_rr   = avg_win / abs(avg_loss) if avg_loss != 0 else 0.0

    return {
        "total_pnl":           round(total_pnl, 2),
        "total_trades":        total_trades,
        "win_rate":            round(win_rate, 4),
        "avg_pnl_per_trade":   round(avg_pnl, 2),
        "long_pnl":            round(long_pnl, 2),
        "short_pnl":           round(short_pnl, 2),
        "sharpe":              round(sharpe, 4),
        "max_drawdown":        round(max_dd, 2),
        "calmar":              round(calmar, 4),
        "avg_rr":              round(avg_rr, 4),
        "exit_type_breakdown": exit_types,
        "trade_records":       trades,
    }
```

**Fold loop** (mirrors `train.py` fold loop pattern exactly — same `_compute_fold_params`, same print format):
```python
if __name__ == "__main__":
    dfs = load_futures_data()
    mnq_df = dfs["MNQ"]
    mes_df = dfs["MES"]
    # ... fold loop identical to train.py structure
    # print lines: fold{i}_train_total_pnl, fold{i}_test_total_pnl, etc.
```

**Validation**: `uv run python train_smt.py` (with futures data cached)

---

### Phase 4: Integration, Tests, and Documentation

#### Task 4.1: CREATE `tests/test_smt_backtest.py`

**Purpose**: Integration tests for `run_backtest()` using synthetic 1m parquet fixtures.

**Approach**: Generate synthetic 1m DataFrames with known SMT divergence scenarios, write to tmp dir, run `run_backtest()`.

| # | Test name | What it verifies |
|---|-----------|-----------------|
| 25 | `test_run_backtest_empty_data_returns_zero_trades` | No bars → 0 trades, no crash |
| 26 | `test_run_backtest_long_trade_tp_hit` | Bullish SMT → TP hit → positive PnL |
| 27 | `test_run_backtest_short_trade_stop_hit` | Bearish SMT → stop hit → negative PnL |
| 28 | `test_run_backtest_session_force_exit` | Open at SESSION_END → "session_close" exit type |
| 29 | `test_run_backtest_end_of_backtest_exit` | Position at date boundary → "end_of_backtest" |
| 30 | `test_pnl_long_correct` | Long PnL = (exit - entry) × contracts × 2.0 |
| 31 | `test_pnl_short_correct` | Short PnL = (entry - exit) × contracts × 2.0 |
| 32 | `test_one_trade_per_day_max` | Two divergences in same day → only first one traded |
| 33 | `test_fold_loop_smoke` | With 4 months of synthetic data, fold loop runs without error |
| 34 | `test_metrics_shape` | Returned dict has all required keys |

**Run**: `uv run pytest tests/test_smt_backtest.py -v`

---

#### Task 4.2: UPDATE `tests/conftest.py` + CREATE `program_smt.md`

**conftest.py change**: Add a `pytest_configure` bootstrap for the futures manifest (mirrors equity manifest bootstrap):
```python
# In pytest_configure(), also bootstrap futures_manifest.json:
futures_cache_dir = os.environ.get(
    "FUTURES_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
)
futures_manifest_path = os.path.join(futures_cache_dir, "futures_manifest.json")
if not os.path.exists(futures_manifest_path):
    os.makedirs(futures_cache_dir, exist_ok=True)
    with open(futures_manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "tickers": ["MNQ", "MES"],
            "backtest_start": "2024-09-01",
            "backtest_end": "2026-03-20",
            "fetch_interval": "1m",
            "source": "ib",
        }, f, indent=2)
```

**program_smt.md**: Write optimization agent instructions mirroring `program.md` but targeting `train_smt.py`. Include:
- Session setup instructions (BACKTEST_START, TRAIN_END, WALK_FORWARD_WINDOWS, etc.)
- Editable constants and functions list
- Forbidden changes (harness below boundary)
- Output format for the agent to parse (same `fold{i}_train_*` key pattern)
- Evaluation criteria (min_test_pnl, win_rate floor, trade count floor)
- "Run: `uv run python train_smt.py`"

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: ADD ContFuture support to `data/sources.py`

- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [1.2, 2.1, 2.2]
- **PROVIDES**: `IBGatewaySource.fetch(..., contract_type="contfuture")` working implementation
- **IMPLEMENT**: Add `contract_type: str = "stock"` param; branch on `ContFuture` vs `Stock`; set `useRTH=False` for futures
- **PATTERN**: `data/sources.py` lines 117–194 (existing `fetch()` implementation)
- **VALIDATE**: `uv run python -c "from data.sources import IBGatewaySource; print('ok')"`

#### Task 1.2: CREATE `prepare_futures.py`

- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Script to download MNQ + MES 1m bars; `futures_manifest.json`
- **IMPLEMENT**: See outline in Phase 1 / Task 1.2 above
- **PATTERN**: `prepare.py` lines 1–120 (config pattern), `process_ticker()`, `write_manifest()`
- **VALIDATE**: `uv run python -c "import prepare_futures; print('import ok')"`

**Wave 1 Checkpoint**: `uv run python -c "from data.sources import IBGatewaySource; import prepare_futures; print('wave1 ok')"`

---

### WAVE 2: Strategy Core

#### Task 2.1: CREATE strategy functions in `train_smt.py` (editable section)

- **WAVE**: 2
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: All strategy functions: `detect_smt_divergence`, `find_entry_bar`, `compute_tdo`, `screen_session`, `manage_position`, plus constants block
- **IMPLEMENT**: All function bodies as specified in Phase 2 / Task 2.1; include `# DO NOT EDIT BELOW THIS LINE` boundary comment at the end of this section
- **PATTERN**: `train.py` lines 1–472 (editable section structure)
- **VALIDATE**: `uv run python -c "import train_smt; print('import ok')"`

#### Task 2.2: CREATE `tests/test_smt_strategy.py`

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: 24 unit tests (tests 1–24 in table above)
- **IMPLEMENT**: `_make_1m_bars()` helper; all 24 test cases
- **PATTERN**: `tests/test_screener.py` (unit test structure, pytest fixtures)
- **VALIDATE**: `uv run pytest tests/test_smt_strategy.py -v`

**Wave 2 Checkpoint**: `uv run pytest tests/test_smt_strategy.py -x`

---

### WAVE 3: Harness

#### Task 3.1: CREATE harness section in `train_smt.py` (below boundary)

- **WAVE**: 3
- **AGENT_ROLE**: harness-engineer
- **DEPENDS_ON**: [2.1, 1.2]
- **BLOCKS**: [4.1]
- **PROVIDES**: `load_futures_data()`, `run_backtest()`, `_compute_metrics()`, fold loop, `__main__` print block
- **IMPLEMENT**: All harness functions as specified in Phase 3 / Task 3.1 above
- **PATTERN**: `train.py` lines 472–1044 (harness structure, fold loop, print format)
- **NOTE**: Fold loop must print `fold{i}_train_total_pnl`, `fold{i}_test_total_pnl` etc. on separate lines for agent parsing
- **VALIDATE**: `uv run python -c "import train_smt; print('harness import ok')"`

**Wave 3 Checkpoint**: `uv run python -c "import train_smt; print('full import ok')"`

---

### WAVE 4: Integration

#### Task 4.1: CREATE `tests/test_smt_backtest.py`

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: []
- **PROVIDES**: 10 integration tests (tests 25–34 in table above)
- **IMPLEMENT**: Synthetic 1m bar generator; all 10 test scenarios; set FUTURES_CACHE_DIR env var in fixtures
- **PATTERN**: `tests/test_backtester.py` (integration test structure)
- **VALIDATE**: `uv run pytest tests/test_smt_backtest.py -v`

#### Task 4.2: UPDATE `tests/conftest.py` + CREATE `program_smt.md`

- **WAVE**: 4
- **AGENT_ROLE**: integration-engineer
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: []
- **PROVIDES**: Futures manifest bootstrap in conftest; optimization agent instructions in program_smt.md
- **IMPLEMENT**: Add futures bootstrap to `pytest_configure()`; write `program_smt.md` mirroring `program.md`
- **PATTERN**: `tests/conftest.py` lines 136–159 (manifest bootstrap); `program.md` (agent instructions)
- **VALIDATE**: `uv run pytest tests/ --collect-only 2>&1 | grep "ERROR" | wc -l` (should be 0)

**Wave 4 / Final Checkpoint**: `uv run pytest tests/ -x`

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_strategy.py` | **Run**: `uv run pytest tests/test_smt_strategy.py -v`

Tests 1–24 (all listed in Task 2.2 table). No IB connection needed — all use synthetic DataFrames.

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_backtest.py` | **Run**: `uv run pytest tests/test_smt_backtest.py -v`

Tests 25–34 (all listed in Task 4.1 table). Use synthetic 1m parquet fixtures written to tmpdir. No IB connection needed.

### Data Source Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_data_sources.py` (UPDATE existing file) | **Run**: `uv run pytest tests/test_data_sources.py -v`

Add 2 tests to existing `test_data_sources.py`:
- `test_ibgateway_contfuture_uses_correct_contract` — mock `ib_insync.ContFuture`, verify it's instantiated
- `test_ibgateway_stock_contract_unchanged` — verify `contract_type="stock"` still uses `Stock`

### Regression Tests

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `uv run pytest tests/ -x`

Full suite must pass with no regressions. The equity harness (`train.py`) is untouched; `test_optimization.py` GOLDEN_HASH must not change.

### Manual Tests (required — cannot automate without live IB connection)

#### Manual Test 1: Live IB Data Download

**Why Manual**: Requires IB-Gateway running and authenticated — cannot automate in CI without a live IB account.
**Steps**:
1. Start IB-Gateway on port 4002
2. `FUTURES_CACHE_DIR=/tmp/test_futures uv run prepare_futures.py`
3. Verify `ls /tmp/test_futures/1m/` shows `MNQ.parquet` and `MES.parquet`
4. `python -c "import pandas as pd; df = pd.read_parquet('/tmp/test_futures/1m/MNQ.parquet'); print(df.shape, df.index[0], df.index[-1])"`
**Expected**: DataFrames with tz-aware ET DatetimeIndex, thousands of rows, columns `[Open, High, Low, Close, Volume]`

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 24 | 69% |
| ✅ Integration (pytest) | 10 | 29% |
| ✅ Data source (pytest) | 2 | 6% |
| ⚠️ Manual (live IB) | 1 | 3% |
| **Total** | 37 | 100% |

**Manual test justification**: Live IB-Gateway connection required; CI environment cannot authenticate with IB API.

---

## VALIDATION COMMANDS

### Level 1: Syntax & Imports

```bash
uv run python -c "from data.sources import IBGatewaySource; import prepare_futures; import train_smt; print('all imports ok')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_smt_strategy.py tests/test_data_sources.py -v
```

### Level 3: Integration Tests

```bash
uv run pytest tests/test_smt_backtest.py -v
```

### Level 4: Full Suite (no regressions)

```bash
uv run pytest tests/ -x
```

---

## ACCEPTANCE CRITERIA

- [ ] `IBGatewaySource.fetch()` accepts `contract_type="contfuture"` and uses `ContFuture("MNQ"/"MES", "CME", "USD")` with `useRTH=False`
- [ ] `prepare_futures.py` imports without error and contains all config constants
- [ ] `train_smt.py` imports without error; `# DO NOT EDIT BELOW THIS LINE` boundary present
- [ ] All 5 strategy functions implemented: `detect_smt_divergence`, `find_entry_bar`, `compute_tdo`, `screen_session`, `manage_position`
- [ ] All 24 unit tests in `test_smt_strategy.py` pass
- [ ] All 10 integration tests in `test_smt_backtest.py` pass
- [ ] 2 new data source tests pass
- [ ] Full test suite passes with 0 new failures (`uv run pytest tests/ -x`)
- [ ] GOLDEN_HASH in `test_optimization.py` unchanged (train.py harness untouched)
- [ ] `program_smt.md` exists and mirrors `program.md` structure
- [ ] `conftest.py` bootstraps `futures_manifest.json` alongside equity manifest

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] Level 1–4 validation all pass
- [ ] 36 automated tests created and passing
- [ ] Manual IB test documented with instructions
- [ ] Full test suite passes (unit + integration)
- [ ] No linting/type errors on modified files
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**TDO proxy**: For signals detected before 9:30 AM ET, `compute_tdo()` returns the first available bar's open for that day as a proxy. If the position is not yet entered by 9:30 AM, the harness should attempt to use the real TDO once the 9:30 bar becomes available. In the MVP, the proxy TDO is used for simplicity.

**Ticker naming**: IB ContFuture symbols are `MNQ` and `MES` (without the TradingView `1!` suffix). Cache files are `MNQ.parquet` and `MES.parquet`. The user refers to them as MNQ1! / MES1! in conversation — internally always use `MNQ` / `MES`.

**useRTH for futures**: CME equity index futures RTH in IB excludes the 9:00–9:30 AM ET window. `useRTH=False` is required to capture the full kill zone from 9:00 AM. The backtester slices to `[SESSION_START, SESSION_END]` so overnight bars are ignored.

**One trade per day**: The harness only opens a new position if none is open. If the session ends with a position open (force-close), the next day starts fresh. This means at most one trade per day.

**Position sizing**: `contracts = max(1, floor(RISK_PER_TRADE / (|entry - stop| × MNQ_PNL_PER_POINT)))`. For typical stop distances (~20–50 pts), `RISK_PER_TRADE=50` yields 0–1 contract → always min 1.
