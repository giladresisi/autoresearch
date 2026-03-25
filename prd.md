# Product Requirements Document: Stock Strategy Backtester with LLM Optimization

## Executive Summary

**autoresearch** is a CLI-based autonomous research system that uses an LLM agent to iteratively optimize a stock trading strategy through backtesting. The system replaces the original nanochat pretraining loop with a fully CPU-based stock screener, position manager, and backtester — no GPU required.

The LLM agent modifies a single Python script (`train.py`) that embodies a complete trading strategy, runs the backtester against a fixed historical dataset, and uses the resulting Sharpe ratio to decide whether to keep or revert the change. This loop runs indefinitely until manually stopped.

**MVP Goal:** Enable autonomous LLM-driven experimentation on a configurable stock screening and position management strategy, with Sharpe ratio as the optimization target.

The system is developed in four versioned harness generations, each building on the previous:

| Version | Scope | Date |
|---------|-------|------|
| **V1** | Initial implementation: data pipeline, backtester, Sharpe metric, agent loop | 2026-01 |
| **V2** | Train/test split, P&L objective, final outputs, sector registry, strategy selector | 2026-03-20 |
| **V3** | Harness robustness: look-ahead fix, risk-proportional sizing, walk-forward CV, robustness controls, configurable fold sizing, test-universe holdout, harness integrity | 2026-03-22 |
| **V4** | Signal quality and measurement fidelity: earnings filter, fallback-stop rejection, time-based exit, fold window fix, objective function fixes, win/loss tracking, post-Phase-1 cooldown | 2026-03-23 |
| **V5** | Instrumentation and data correctness: price fix (Close not Open), MFE/MAE, exit-type tagging, R-multiple | 2026-03-24 |
| **V6** | Signal coverage expansion | 2026-03-25 |

---

## Mission

Enable fully autonomous, reproducible iteration on a stock trading strategy by giving an LLM a clean feedback loop: modify strategy → run backtest → observe Sharpe ratio → keep or revert.

### Core Principles

1. **Separation of concerns** — Data download (`prepare.py`) is fixed and never modified by the agent. Strategy logic (`train.py`) is the only file the agent touches.
2. **Reproducibility** — Data is downloaded once and cached. All runs on the same dataset produce deterministic results for the same code.
3. **Fast iteration** — Each backtest run completes in seconds, enabling many experiments per hour.
4. **No GPU required** — All computation is pure Python / pandas / numpy. Runs on any developer machine.
5. **Simplicity over cleverness** — A simpler strategy that performs slightly better is preferred over a complex one with marginal gains.

---

## Target Users

**The primary user is the developer/trader** who sets up the system, configures tickers and date range, then lets it run autonomously.

**Technical comfort level:** High — comfortable with Python, git, command-line tools, and basic algorithmic trading concepts (screeners, stop-losses, Sharpe ratio).

**What the user wants:**
- To explore whether LLM-driven strategy search can find improvements to a manually designed screener
- To observe what parameter changes and logic modifications improve risk-adjusted returns
- To run experiments overnight or while away from the machine

---

## MVP Scope

### In Scope

**Core Functionality:**
- `prepare.py`: Download and cache historical OHLCV + 10am price data for a fixed ticker list via yfinance
- `train.py`: Screener + position manager + backtester producing a single Sharpe ratio output
- `program.md`: Agent instructions for the autonomous optimization loop
- `results.tsv`: Experiment log (commit, Sharpe, trade count, status, description)

**Technical:**
- 1h interval yfinance data, resampled to daily OHLCV + `price_10am` column
- Fractional share sizing ($500 per position)
- Stop-hit detection via previous day's Low
- Daily mark-to-market portfolio valuation
- Annualized Sharpe ratio (risk-free rate = 0%, based on daily dollar P&L changes)

**Strategy (baseline):**
- Screener logic ported from `example-screener.py` v2 (SMA150, CCI, ATR, volume, pullback, wick, pivot-low stop, resistance filter)
- Position manager: raise stop to breakeven when price ≥ entry + 1×ATR14

**Infrastructure:**
- `pyproject.toml` updated: remove GPU/NLP deps, add yfinance
- Existing branch/commit/loop model preserved from original autoresearch

### Out of Scope (Future Considerations)

- Short selling
- Multiple simultaneous entries in the same ticker
- Transaction costs beyond $0.03/share slippage
- Intraday (sub-daily) strategy logic
- Live trading integration
- Portfolio-level risk limits (max drawdown stop, max concurrent positions cap)
- Dynamic ticker universe fetched per-run (S&P 500 Wikipedia scrape)
- Percentage-return-based Sharpe (vs. dollar-P&L-based)

---

## User Stories

### Primary User Stories

1. **As a user, I want to configure tickers and a date range once**, so that all subsequent experiments use the same dataset without re-downloading.
   - Example: Set `TICKERS = ["AAPL", "MSFT", "NVDA"]` and `BACKTEST_START = "2026-01-01"` in `prepare.py`, run it once, then run `train.py` many times.

2. **As a user, I want the backtester to simulate realistic trading**, so that Sharpe ratio reflects actual strategy performance rather than look-ahead bias.
   - Example: On day T, the screener only sees data up to day T. Stop hits are checked using the Low of the current day. Entry uses `price_10am + $0.03`.

3. **As a user, I want the agent to log every experiment result**, so that I can review the history of what worked and what didn't after the session.
   - Example: `results.tsv` has a row per experiment with commit hash, Sharpe, trade count, keep/discard status, and description.

4. **As a user, I want each backtest run to finish in seconds**, so that the agent can run dozens of experiments per hour.
   - Example: ~42 trading days × 100 tickers completes in under 10 seconds on a modern laptop.

### Technical User Stories

5. **As the LLM agent, I want a single clearly defined output metric**, so that I can compare experiments without ambiguity.
   - Example: `train.py` always prints `sharpe: 1.234567` in a parseable format at the end.

6. **As the LLM agent, I want `prepare.py` to be read-only**, so that I can understand the data format without risk of accidentally breaking the data pipeline.

---

## Core Architecture & Patterns

### Architecture

Single-repository, two-script design. The data pipeline and the strategy are deliberately isolated into separate files with a clear contract between them.

```
autoresearch/
├── prepare.py          # Fixed: data download + cache. Never modified by agent.
├── train.py            # Mutable: screener + position manager + backtester.
├── program.md          # Agent instructions for the optimization loop.
├── results.tsv         # Untracked experiment log (not committed).
├── initial_request.md  # Project origin context.
├── prd.md              # This document.
├── example-screener.py # Reference implementation (read-only, not used at runtime).
├── pyproject.toml      # Dependencies.
└── ~/.cache/autoresearch/stock_data/
    ├── AAPL.parquet    # One file per ticker.
    ├── MSFT.parquet
    └── ...
```

### Key Patterns

**1. Fixed data / mutable strategy split**
- `prepare.py` is the ground truth for what data exists. It defines `TICKERS`, `BACKTEST_START`, `BACKTEST_END`, and the cache location. The agent reads these constants but never edits the file.
- `train.py` loads data from cache; the agent modifies strategy logic freely within this file.

**2. Chronological backtest loop with no look-ahead**
- On day T, the screener receives `history_df.loc[:T]` — only data up to and including T. Price at T is `price_10am[T]`.
- Stop checks use the Low of day T (the day after a position was opened or managed), ensuring the stop price was valid at some point during day T.

**3. Git as experiment versioning**
- Every experiment is a git commit. Keeping an experiment means staying on that commit. Reverting means `git reset --hard HEAD~1`.
- `results.tsv` is intentionally untracked (not committed), so it accumulates across resets.

**4. Parseable terminal output**
- `train.py` prints a fixed-format summary block starting with `---`. The agent extracts the key metric with `grep "^sharpe:" run.log`.

---

## Features

### Feature 1: Data Download (`prepare.py`)

**Description:** One-time download of historical OHLCV data for a user-configured ticker list and date range.

**Components:**
- `TICKERS` constant (placeholder list for user to fill)
- `BACKTEST_START` / `BACKTEST_END` date constants
- `HISTORY_START` derived constant (2 years before `BACKTEST_START`, for indicator warmup)
- Per-ticker download via `yf.download(ticker, start=HISTORY_START, end=BACKTEST_END, interval="1h")`
- Resampling to daily: `open` (first bar), `high` (max), `low` (min), `close` (last bar), `volume` (sum), `price_10am` (Open of 10:00 AM ET bar)
- Output: one Parquet file per ticker at `~/.cache/autoresearch/stock_data/{TICKER}.parquet`
- Idempotent: skips tickers whose file already exists
- Validation warnings: < 200 rows (insufficient indicator history), missing `price_10am` on backtest dates

**Data schema per ticker Parquet file:**

| Column | Type | Description |
|--------|------|-------------|
| `date` | date | Trading day (index) |
| `open` | float | First bar open of the day (9:30 AM ET) |
| `high` | float | Daily high (max across all hourly bars) |
| `low` | float | Daily low (min across all hourly bars) — used for stop detection |
| `close` | float | Last bar close of the day |
| `volume` | float | Total daily volume (sum of all hourly bars) |
| `price_10am` | float | Open of the 10:00 AM ET bar — used as screener/entry price |

---

### Feature 2: Screener (`train.py` — `screen_day` function)

**Description:** Applies a set of criteria to each ticker's daily history to identify entry signals on a given day.

**Baseline criteria (from `example-screener.py` v2):**

| ID | Rule | Details |
|----|------|---------|
| R1 | Minimum history | ≥ 150 rows (SMA150 must be defined) |
| 1  | Above SMA150 | `price_10am > SMA150` |
| 2  | 3 consecutive up days | Each of last 3 closes > prior close |
| 3  | Volume ≥ 0.85× MA30 | For both of last 2 days |
| 4  | CCI(20) < −50, rising | CCI rising on last 2 consecutive days |
| 5  | Pullback ≥ 8% | From 7-day local high AND all-time high |
| R4 | Candle wick | Upper wick < body of the entry candle |
| R3 | Not stalling at ceiling | Last 3 highs tight, all closes below them |
| R2+R6 | Pivot-low stop valid | Valid pivot low exists, ≤ 10 zone touches in 90 days |
| 1.5× | ATR buffer | `price_10am − stop ≥ 1.5 × ATR14` |
| R5 | Resistance clearance | Nearest pivot high ≥ 2× ATR above entry |

**Interface:**
```python
def screen_day(df: pd.DataFrame, today: date) -> dict | None:
    """
    df: full daily history up to and including today
    Returns None if no signal, or dict with at minimum {'stop': float}
    """
```

---

### Feature 3: Position Manager (`train.py` — `manage_position` function)

**Description:** Updates the stop price for a held position based on current market state.

**Baseline logic:** Raise stop to breakeven (`entry_price`) once `price_10am[T] >= entry_price + 1 × ATR14`. Never lower the stop.

**Interface:**
```python
def manage_position(position: dict, df: pd.DataFrame) -> float:
    """
    position: {'entry_price', 'entry_date', 'shares', 'stop_price', 'ticker'}
    df: full daily history up to and including today
    Returns updated stop_price (must be >= position['stop_price'])
    """
```

---

### Feature 4: Backtester Loop (`train.py`)

**Description:** Chronological simulation of the strategy across all trading days in the backtest window.

**Loop steps per trading day T:**

1. **Check stops**: For each open position, if previous day's `low ≤ stop_price` → close position at stop price, record realized P&L.
2. **Screen**: Run `screen_day` on each ticker not already in portfolio. For each signal: buy at `price_10am[T] + 0.03`, `shares = 500.0 / entry_price`.
3. **Manage**: Run `manage_position` on all open positions (including new ones). Apply returned stop (if higher than current).
4. **Mark-to-market**: Record daily portfolio value = `Σ(price_10am[T] × shares)` for all open positions.

**End of backtest:** Close all remaining open positions at their last available `price_10am`. Add realized P&L.

---

### Feature 5: Sharpe Ratio Output (`train.py`)

**Description:** Compute and print the optimization metric.

**Computation:**
```python
daily_pnl_changes = np.diff(daily_portfolio_values)  # day-over-day mark-to-market change
sharpe = (daily_pnl_changes.mean() / daily_pnl_changes.std()) * np.sqrt(252)
# If std == 0 (no activity): sharpe = 0.0
```

**Output block (fixed format, parsed by agent):**
```
---
sharpe:              1.234567
total_trades:        12
win_rate:            0.583
avg_pnl_per_trade:   18.45
total_pnl:           221.40
backtest_start:      2026-01-01
backtest_end:        2026-03-01
```

Exit code 0 on success, exit code 1 on crash.

---

### Feature 6: Optimization Loop (`program.md`)

**Description:** Agent instructions for the autonomous experimentation session.

**Setup phase (once):**
1. Agree on a run tag (e.g., `mar18`). Create branch `autoresearch/<tag>`.
2. Read `prepare.py`, `train.py`, `program.md` for full context.
3. Verify `~/.cache/autoresearch/stock_data/` is populated. If not, instruct user to run `uv run prepare.py`.
4. Initialize `results.tsv` with header row.

**Experiment loop (forever):**
1. Check git state.
2. Modify `train.py` (screener thresholds, criteria, position manager logic, indicator parameters).
3. `git commit`
4. `uv run train.py > run.log 2>&1`
5. `grep "^sharpe:" run.log` — if empty, crash → `tail -n 50 run.log` → fix or skip.
6. Log to `results.tsv`.
7. If Sharpe improved (higher) → keep. If not → `git reset --hard HEAD~1`.
8. Never pause to ask the user. Loop until manually stopped.

**results.tsv columns:** `commit`, `sharpe`, `total_trades`, `status` (`keep`/`discard`/`crash`), `description`

**What the agent CAN modify:** anything in `train.py` — screener criteria, thresholds, indicator parameters, position manager logic, entry/exit rules.

**What the agent CANNOT modify:** `prepare.py`, `TICKERS`, `BACKTEST_START`, `BACKTEST_END`, the output format block, or the Sharpe computation formula.

---

## Technology Stack

### Core Runtime

- **Python** (≥ 3.10) — primary language
- **uv** — package manager and script runner (`uv run train.py`)

### Data

- **yfinance** (latest) — historical OHLCV data via Yahoo Finance API
- **pandas** (≥ 2.x) — data manipulation, resampling, indicator computation
- **numpy** (≥ 2.x) — numerical operations, Sharpe computation
- **pyarrow** (≥ 21.x) — Parquet read/write for cached data

### Visualization (optional, existing)

- **matplotlib** (≥ 3.x) — available for analysis notebooks; not used by `train.py`

### Dev Tools

- **git** — experiment versioning
- **uv** — dependency isolation

### Removed Dependencies

The following packages from the original `pyproject.toml` are no longer needed and will be removed:
- `torch`, `kernels` (GPU training)
- `rustbpe`, `tiktoken` (BPE tokenizer)
- `requests` (was used for parquet shard downloads; yfinance handles its own HTTP)

---

## Parallel Execution Architecture

This is a single-developer / single-agent project. No parallel workstreams are needed during the autonomous experiment loop — one agent runs sequentially (modify → test → log → repeat). However, the **implementation build** can be parallelized across two independent workstreams:

### Workstream A: Data Layer
**Scope:** `prepare.py` rewrite + `pyproject.toml` update
**Dependencies:** None
**Deliverables:** Working `prepare.py` that populates `~/.cache/autoresearch/stock_data/*.parquet` with the correct schema

### Workstream B: Strategy + Backtester
**Scope:** `train.py` rewrite + `program.md` rewrite
**Dependencies:** Requires the Parquet schema from Workstream A to be finalized
**Interface contract from A:** Column names (`date`, `open`, `high`, `low`, `close`, `volume`, `price_10am`) and index type (date)
**Deliverables:** Working `train.py` that reads from the cache and outputs the fixed summary block

### Synchronization Point

After both workstreams complete: end-to-end test — run `prepare.py` with a small ticker list (e.g., 3 tickers), then run `train.py` and verify the `sharpe:` line appears in output.

---

## Security & Configuration

### Configuration Management

All user-configurable constants live at the top of `prepare.py` and are clearly marked:

```python
# ── USER CONFIGURATION ─────────────────────────────────────────────────────
TICKERS = []  # TODO: fill in ticker symbols, e.g. ["AAPL", "MSFT", "NVDA"]

BACKTEST_START = "2026-01-01"  # first day of the backtest window (inclusive)
BACKTEST_END   = "2026-03-01"  # last day of the backtest window (exclusive)
# ───────────────────────────────────────────────────────────────────────────

# Derived (do not modify)
HISTORY_START = (pd.Timestamp(BACKTEST_START) - pd.DateOffset(years=2)).strftime("%Y-%m-%d")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
```

### No Credentials Required

yfinance accesses Yahoo Finance's public API without authentication. No API keys, `.env` files, or secrets management needed.

### Data Privacy

All data is stored locally in `~/.cache/autoresearch/`. No data is sent to any external service during backtesting.

---

## API Specification

### External API: yfinance

**Library:** `yfinance` (Python wrapper around Yahoo Finance)
**Authentication:** None required
**Purpose:** Download historical OHLCV data for stock tickers

**Usage pattern in `prepare.py`:**
```python
import yfinance as yf

ticker_obj = yf.Ticker(ticker)
df = ticker_obj.history(
    start=HISTORY_START,
    end=BACKTEST_END,
    interval="1h",
    auto_adjust=True,
    prepost=False,
)
# df has columns: Open, High, Low, Close, Volume
# df.index is DatetimeIndex in UTC; localize to America/New_York
```

**Key behaviors:**
- `interval="1h"` provides hourly bars; supported for up to ~730 days of history
- `auto_adjust=True` adjusts for splits and dividends
- `prepost=False` excludes pre/post-market bars
- Returns empty DataFrame for invalid tickers or tickers with no data in range
- Timestamps in UTC by default — must convert to `America/New_York` to identify the 10:00 AM bar

**10am bar extraction:**
```python
df.index = df.index.tz_convert("America/New_York")
# Filter to the 10:00 AM bar for each day
df_10am = df[df.index.time == pd.Timestamp("10:00").time()]
# price_10am[date] = df_10am.loc[date, "Open"]
```

**Compatibility notes:**
- yfinance 1h data limit: ~730 days. For `BACKTEST_START = "2026-01-01"`, `HISTORY_START` would be `"2024-01-01"` — within the supported window as of March 2026.
- yfinance is an unofficial wrapper; Yahoo Finance may change their API without notice. If download fails for a ticker, log a warning and skip.

---

## Success Criteria

### MVP Success Definition

**Functional Requirements:**
- `prepare.py` downloads data for all configured tickers and stores valid Parquet files
- `train.py` runs without error on the cached data and prints a `sharpe:` value
- The backtester has no look-ahead bias (screener only sees data up to day T)
- Stop-hit detection correctly uses the Low of the relevant day
- The agent loop in `program.md` produces a coherent optimization trajectory in `results.tsv`

**Quality Indicators:**
- Backtest for ~42 trading days × 100 tickers completes in < 30 seconds
- No crashes on edge cases: no matches found, all positions stopped out on day 1, only 1 trading day
- Sharpe ratio is finite and correctly computed when trades occur; returns 0.0 when no trades occur

**User Experience:**
- User can configure TICKERS + date range, run `prepare.py` once, then hand off to the agent
- `results.tsv` is human-readable and shows clear experiment progression
- Agent never needs to re-download data during the experiment loop

---

## V1: Initial Implementation (Phases 1–5)

### Phase 1: Infrastructure Setup

**Goal:** Update `pyproject.toml`, verify yfinance works in the project environment.

**Tasks:**
- Remove `torch`, `kernels`, `rustbpe`, `tiktoken` from `pyproject.toml`
- Add `yfinance`
- Run `uv sync` and verify install
- Quick smoke test: `python -c "import yfinance as yf; print(yf.Ticker('AAPL').history(period='5d', interval='1h').tail())"`

**Validation:**
```bash
uv run python -c "import yfinance, pandas, numpy, pyarrow; print('OK')"
```

---

### Phase 2: Data Layer (`prepare.py`)

**Goal:** Rewrite `prepare.py` to download, process, and cache stock OHLCV data.

**Tasks:**
- Rewrite `prepare.py` per the Feature 1 specification
- Include user-configurable constants block at top
- Implement hourly → daily resampling with `price_10am` extraction
- Implement per-ticker Parquet caching (idempotent)
- Add validation warnings for insufficient history or missing 10am bars

**Validation:**
```bash
# Set TICKERS = ["AAPL", "MSFT"] in prepare.py, then:
uv run prepare.py
# Verify: ls ~/.cache/autoresearch/stock_data/
# Verify schema: python -c "import pandas as pd; print(pd.read_parquet('~/.cache/autoresearch/stock_data/AAPL.parquet').head())"
```

---

### Phase 3: Strategy + Backtester (`train.py`)

**Goal:** Rewrite `train.py` with the baseline screener, position manager, and backtester.

**Tasks:**
- Port indicator functions from `example-screener.py` (SMA, CCI, ATR, pivot logic, etc.)
- Implement `screen_day()` with all v2 criteria
- Implement `manage_position()` with breakeven stop logic
- Implement chronological backtester loop (stop check → screen → manage → mark-to-market)
- Implement Sharpe ratio computation
- Print fixed-format summary block

**Validation:**
```bash
uv run train.py 2>&1 | tee run.log
grep "^sharpe:" run.log
# Expected: sharpe: <some number> (may be 0.0 if no trades in small date range)
```

---

### Phase 4: Agent Instructions (`program.md`)

**Goal:** Rewrite `program.md` with updated agent instructions for the stock optimization loop.

**Tasks:**
- Update setup section (branch creation, read prepare.py + train.py, verify cache)
- Update experiment loop (run `train.py`, grep `sharpe:`, log to `results.tsv`)
- Update results.tsv schema (`commit`, `sharpe`, `total_trades`, `status`, `description`)
- Update what the agent can/cannot modify
- Clarify optimization direction: higher Sharpe = better (opposite of original val_bpb)

**Validation:** Human review — does it clearly describe the loop? Does it match the output format from Phase 3?

---

### Phase 5: End-to-End Test

**Goal:** Verify the complete pipeline works together.

**Tasks:**
- Set `TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]` in `prepare.py`
- Run `prepare.py`, verify 5 Parquet files created
- Run `train.py`, verify clean output with `sharpe:` line
- Manually verify one trade's P&L calculation is correct
- Simulate the first two steps of the agent loop (modify a threshold, rerun, compare Sharpe)

---

## Future Considerations (Post-MVP)

**Potential Enhancements:**
- Configurable risk-free rate for Sharpe computation
- Max concurrent positions cap (portfolio-level risk control)
- Max drawdown circuit breaker
- Percentage-return-based Sharpe (normalized by capital deployed per day)
- Exit-only days tracking (do not enter new positions on high-volatility days)

**Integration Opportunities:**
- Live screening mode: run `screen_day` on today's data and print actionable signals
- Export to CSV: trade log with entry/exit dates, prices, P&L per trade
- `analysis.ipynb` integration: load `results.tsv` and plot Sharpe progression over experiments

**Advanced Features:**
- Multi-strategy ensemble (run multiple `train.py` variants on different branches simultaneously)
- Dynamic ticker universe (re-fetch S&P 500 constituents at prepare time)
- Walk-forward validation → **addressed in V3-B (R2)**
- Risk-per-trade sizing → **addressed in V3-A (R3)**
- Max concurrent positions cap → **addressed in V3-C (R8)**

---

## Risks & Mitigations

### Risk: No trades in backtest window

With only ~42 trading days (Jan 1 – Mar 1, 2026) and a strict screener, it's possible that very few matches occur, producing a statistically noisy or zero Sharpe. The agent may have limited signal to optimize against.

**Mitigation:**
- User should extend the date range to at least 6–12 months once the pipeline is verified
- The agent can relax screener criteria early to find at least some trades
- Phase 5 validation explicitly checks for at least 1 trade before handing off to the agent

---

### Risk: yfinance API instability

yfinance is an unofficial Yahoo Finance scraper. Yahoo occasionally changes the API or rate-limits aggressively, causing download failures.

**Mitigation:**
- `prepare.py` logs warnings per failed ticker and continues (doesn't abort entire download)
- Cached Parquet files are idempotent — partial downloads can be retried
- If yfinance fails entirely: pandas-datareader or direct CSV download from Yahoo are fallbacks

---

### Risk: Look-ahead bias in screener

If the screener accidentally uses data from day T+1 or later (e.g., via `.iloc[-1]` on a rolling window that includes future rows), backtest results will be unrealistically good.

**Mitigation:**
- `prepare.py` produces a clean per-ticker DataFrame with a date index
- `train.py` passes `df.loc[:today]` to the screener, making look-ahead structurally impossible as long as the slice is correct
- End-to-end test includes a sanity check: Sharpe should not be implausibly high (> 5.0) for a strict screener

---

### Risk: Agent optimizes for Sharpe in a way that overfits to ~42 trading days

A very short backtest period allows the agent to "memorize" specific days and overfit aggressively.

**Mitigation:**
- Noted as a known limitation; user should extend date range before long optimization sessions
- Program.md will note a simplicity criterion: prefer interpretable changes over complex ones that may be fitting noise

---

### Risk: Fractional shares complicate P&L math

Allowing fractional shares is mathematically correct but differs from real brokerage behavior (most retail brokers don't allow fractional shares on all tickers).

**Mitigation:**
- Clearly documented as a simplification in `train.py` comments
- P&L math is straightforward: `(exit_price - entry_price) × shares` regardless of fraction
- Realistic behavior can be added later (floor to integer shares)

---

---

## V2: Evaluation Signal Quality (Enhancements 1–6, 2026-03-20)

These additions were identified after running multi-sector optimization loops across five worktrees (energy, semis, utilities, financials, energy OOS). They address discovered limitations in the optimization signal and add new reporting capabilities.

---

### Enhancement 1: Train/Test Split

**Motivation:** Optimizing on the full backtest window leads to overfitting — the agent can memorize specific days and tune thresholds to the noise of a single 3-month period, as confirmed by the energy strategy's in-sample Sharpe of 5.79 collapsing to -0.01 OOS.

**Description:** The backtest window is split at setup time. The final 2 weeks become a held-out test set. The optimization loop runs exclusively on the training set. After each iteration, the strategy is also run on the test set for tracking — but the keep/discard decision is based on train P&L only (no leakage).

**Changes required:**

| File | Section | Change |
|---|---|---|
| `train.py` | Mutable (above line) | Add `TRAIN_END` and `TEST_START` constants (written by agent at setup; `TRAIN_END = BACKTEST_END − 14 calendar days`) |
| `train.py` | Immutable (below line) | `run_backtest(start=None, end=None)` — add optional start/end params defaulting to `BACKTEST_START`/`BACKTEST_END` |
| `train.py` | Immutable (below line) | `print_results(metrics, prefix="")` — add prefix param so output lines become `train_total_pnl:` vs `test_total_pnl:` |
| `train.py` | Immutable (below line) | `__main__` — call `run_backtest` twice (train range, test range) and print both result blocks |
| `program.md` | Setup | Compute and write `TRAIN_END`/`TEST_START` at session start |
| `program.md` | Loop | Keep/discard based on `train_total_pnl`; also record `test_pnl` in results.tsv |
| `program.md` | results.tsv schema | Add `test_pnl` column |

**Design constraint:** The keep/discard decision must never use `test_total_pnl`. Using test results for selection converts the test set into a second training set, negating its purpose.

---

### Enhancement 2: Optimize for Train P&L Instead of Sharpe

**Motivation:** The Sharpe formula in the current backtest (`mean(diff(daily_portfolio_values)) / std(diff(daily_portfolio_values))`) operates on the raw dollar portfolio value series, not on percentage returns relative to capital deployed. On days with no open positions the value is $0, making `diff` include large entry/exit artifacts. This causes two pathologies:

1. **Inflated Sharpe with no activity:** A mostly-idle portfolio has low variance → high Sharpe, regardless of whether P&L is positive.
2. **Degenerate stop management optimum:** Raising the breakeven trigger reduces stop-management events, which smooths the portfolio value series, which lowers variance, which mechanically raises Sharpe — while actually worsening P&L. This was confirmed in the financials run: Sharpe 6.58 with total P&L of -$60.

**Description:** Replace Sharpe with `train_total_pnl` as the keep/discard criterion. This directly rewards strategies that make money on the training window.

**Changes required:**

| File | Section | Change |
|---|---|---|
| `program.md` | Loop | Keep/discard decision: `if train_total_pnl improved (higher) → keep; else → git reset --hard HEAD~1` |
| `program.md` | Loop | Extract `grep "^train_total_pnl:" run.log` instead of `grep "^sharpe:"` |
| `program.md` | results.tsv schema | Primary sort/compare column becomes `train_pnl`; Sharpe still recorded as informational |

**Note:** Sharpe is still computed and printed — it remains useful as a diagnostic signal for risk-adjusted quality, but it no longer drives the optimization decision.

---

### Enhancement 3: Final Test Run Outputs — CSV and Per-Ticker P&L Table

**Motivation:** After the optimization loop completes, the user wants to inspect the raw trade data for the final test run in detail: the full daily OHLCV + indicator DataFrame for each ticker, and a per-ticker P&L breakdown to understand which names contributed to performance.

**Description:** After the final iteration, the agent triggers a special final test run with full output mode enabled. The run writes a CSV of all ticker data and prints a per-ticker P&L table.

**Changes required:**

| File | Section | Change |
|---|---|---|
| `train.py` | Mutable (above line) | Add `WRITE_FINAL_OUTPUTS = False` constant (agent sets to `True` only for the final test run) |
| `train.py` | Immutable (below line) | `run_backtest()` — accumulate `ticker_pnl: dict[str, float]` as trades close |
| `train.py` | Immutable (below line) | At end of backtest, if `WRITE_FINAL_OUTPUTS`: write `final_test_data.csv` (per-ticker daily OHLCV + indicators for the test window) and print per-ticker P&L table |
| `program.md` | End of loop | After final iteration: set `WRITE_FINAL_OUTPUTS = True`, run test window, restore to `False` |

**Output:**
- `final_test_data.csv` — one row per (ticker, date) with all columns the strategy uses
- Printed table: ticker, trades, wins, total P&L, sorted by P&L descending

---

### Enhancement 4: Sector Trend Summary (`data_trend.md`)

**Motivation:** Before the first iteration, the agent should report the general market character of the downloaded tickers for the backtest period — whether the universe was broadly bullish, bearish, or mixed. This provides context for interpreting the strategy results and helps the agent choose appropriate starting directions.

**Description:** `prepare.py` writes a `data_trend.md` file immediately after all data is downloaded. The file contains a one-paragraph summary of the sector's price behavior over the backtest window.

**Changes required:**

| File | Section | Change |
|---|---|---|
| `prepare.py` | Infrastructure (not user config) | Add `write_trend_summary(tickers, backtest_start, backtest_end, cache_dir)` function called after download loop |

**Content of `data_trend.md`:** Ticker count, median return over the window, count of up vs down names, top 3 gainers and bottom 3 losers with % return, and a one-line sector character phrase (e.g., "Broadly bullish: 13/17 tickers rose, median +8.2%. Sector trending up with moderate dispersion.").

---

### Enhancement 5: Extended results.tsv

**Motivation:** The current results.tsv only records `sharpe` and `total_trades`. After running multi-sector loops, the gap between Sharpe and actual P&L was missed because P&L was never tracked per-iteration. Win rate and P&L provide essential additional signal for evaluating experiment quality.

**Description:** Add `win_rate`, `train_pnl`, and `test_pnl` columns to results.tsv. All values are already printed by `print_results()` in run.log — this is purely a program.md change.

**New results.tsv schema:**

```
commit	train_pnl	test_pnl	train_sharpe	total_trades	win_rate	status	description
```

**Changes required:**

| File | Section | Change |
|---|---|---|
| `program.md` | results.tsv schema | Update column spec and grep commands |
| `program.md` | Loop | Extract `win_rate:` and `total_pnl:` from run.log for each block (train/test) |

---

### Enhancement Summary

| # | Enhancement | Mutable section | Immutable section | `program.md` | `prepare.py` |
|---|---|:---:|:---:|:---:|:---:|
| 1 | Train/test split | ✅ (2 constants) | ✅ (params + prefix + __main__) | ✅ | — |
| 2 | Optimize for train P&L | — | — | ✅ | — |
| 3 | Final test outputs (CSV + table) | ✅ (1 constant) | ✅ (accumulate + output) | ✅ | — |
| 4 | Sector trend summary | — | — | — | ✅ |
| 5 | Extended results.tsv | — | — | ✅ | — |

All immutable-section changes are **additive**: optional parameters with backwards-compatible defaults, output gated behind a flag, and a prefix parameter. The Sharpe formula and core backtest loop are unchanged.

---

### Enhancement 6: Multi-Strategy Registry and LLM-Driven Strategy Selector

**Motivation:** Once multiple strategies have been optimized for different sectors and timeframes, they need to coexist in a single git branch and be applied to real-time ticker data. The selection of which strategy applies to a given ticker on a given day is not a mechanical lookup — it requires contextual reasoning about sector fit, regime similarity, and recent price behavior.

---

#### 6a: Strategy Registry

**Description:** A `strategies/` directory on `master` holds one Python module per published strategy. Each file contains the strategy's `screen_day()`, `manage_position()`, and a `METADATA` dict that fully documents its optimization context.

```
strategies/
├── __init__.py              # REGISTRY dict: {name: module}
├── base_indicators.py       # shared: calc_rsi, calc_atr14, calc_cci, find_stop_price, ...
├── energy_momentum_v1.py
├── semis_momentum_v1.py
├── utilities_breakout_v1.py
└── ...
```

**`METADATA` fields per strategy:**

| Field | Description |
|---|---|
| `name` | Unique strategy identifier (e.g. `semis-momentum-v1`) |
| `sector` | Sector label (e.g. `semiconductors`) |
| `tickers` | Ticker universe the strategy was optimized on |
| `train_start` / `train_end` | Training window dates |
| `test_start` / `test_end` | Held-out test window dates (post-Enhancement 1) |
| `source_branch` | Git branch the strategy was optimized on |
| `source_commit` | Git commit hash of the best iteration |
| `train_pnl` | Total P&L on training window |
| `train_sharpe` | Sharpe on training window (informational) |
| `train_trades` | Number of trades on training window |
| `description` | Free-text description of what the strategy does and what market regime it was tuned for |

**Branching policy (enforced from Enhancement 6 onwards):**

| Branch | Allowed changes |
|---|---|
| `master` / `main` | Infrastructure, tooling, harness, data layer, screener, docs, registry extraction (write-only append of `strategies/<name>.py`). **Strategy logic (`screen_day`, `manage_position`, parameters) must not be modified here.** |
| Worktrees (e.g. `autoresearch/<date>`) | All strategy optimization work. The only place where `train.py` is iterated, filters are added/removed, and parameters are tuned. |

**Exceptions:** Strategy code in `master` may be touched only for: (1) critical bug fixes that affect live trading correctness, (2) renaming/deprecating a strategy module, or (3) explicit instruction from the user. These must be noted in the commit message.

**How strategies reach master (no `git merge`):**
Worktree branches are never merged into master. After a worktree's optimization loop completes, a post-optimization extraction step reads the best `train.py` via `git show <tag>:train.py`, extracts all code above the `# DO NOT EDIT BELOW THIS LINE` boundary, and writes it as a new `strategies/<name>.py` on master. The worktree branch and tag remain intact for audit; the branch itself can be pruned after extraction.

This approach avoids merge conflicts entirely — `train.py` across worktrees are structurally different files (each was evolved independently), but each strategy's `screen_day()` and `manage_position()` are self-contained and can coexist as separate modules.

---

#### 6b: LLM-Driven Strategy Selector

**Description:** When screening a ticker for real-time trade signals, the strategy to apply is selected by an LLM based on contextual reasoning — not by a deterministic lookup of which tickers were in the strategy's training universe. The LLM reads the available strategies' metadata and the current ticker's recent data, and decides which strategy (or strategies) is most applicable, explaining its choice.

**Why not a ticker-membership filter:** A strategy optimized on `[NVDA, AMD, INTC, ...]` captures patterns of the semiconductor sector in a specific market regime. A new ticker like `MRVL` (Marvell) wasn't in the training set but belongs to the same sector and may fit the same regime. Conversely, `GS` (Goldman Sachs) was in the financials training set, but if Goldman is behaving like a growth stock during a particular rally, the semis momentum strategy might be more applicable than the financials one. Ticker membership is a weak proxy for relevance; sector fit + regime match is the right criterion.

**Selector inputs:**
- Full `REGISTRY` with each strategy's `METADATA` (sector, training window, description, regime context)
- Target ticker symbol + its recent OHLCV data (e.g. last 30 days: trend direction, volatility, volume pattern)
- Today's date

**Selector output:**
- Selected strategy name (or a ranked list if multiple seem applicable)
- Plain-language explanation covering: which sector the ticker fits, which market regime is currently active, why the selected strategy's training conditions match current conditions, and any caveats

**Example output for NVDA on a given day:**
> *Selected: `semis-momentum-v1`*
> *Reason: NVDA is a semiconductor name and is currently above its 20-day SMA with RSI ~62 and volume trending above its 30-day average — matching the regime this strategy was trained on (Dec–Mar 2026, semis in a momentum phase). The financials strategy (`financials-v1`) is not applicable: different sector, different price dynamics. The utilities strategy is not applicable: NVDA is high-beta, utilities strategy was tuned for low-ATR slow movers.*

**Example output for NVDA on a different day:**
> *Selected: none (no strategy confident match)*
> *Reason: NVDA is trading below SMA20 with RSI 38 — a mean-reversion environment. The semis strategy was optimized for breakout entries in an uptrending regime and would produce false signals here. No currently available strategy was tuned for semiconductor mean-reversion.*

**Implementation:** The selector is a lightweight LLM call (separate from the optimization loop) that receives strategy metadata + ticker snapshot as context. It is not part of `train.py` or the backtesting infrastructure — it is a runtime tool built on top of the registry.

**Interface:**
```python
def select_strategy(ticker: str, recent_df: pd.DataFrame, today: date) -> dict:
    """
    Returns:
      {
        'strategy': 'semis-momentum-v1',   # or None
        'explanation': '...',
        'confidence': 'high' | 'medium' | 'low'
      }
    """
```

---

#### Enhancement Summary (updated)

| # | Enhancement | Mutable section | Immutable section | `program.md` | `prepare.py` | New files |
|---|---|:---:|:---:|:---:|:---:|:---:|
| 1 | Train/test split | ✅ | ✅ | ✅ | — | — |
| 2 | Optimize for train P&L | — | — | ✅ | — | — |
| 3 | Final test outputs (CSV + table) | ✅ | ✅ | ✅ | — | — |
| 4 | Sector trend summary | — | — | — | ✅ | `data_trend.md` |
| 5 | Extended results.tsv | — | — | ✅ | — | — |
| 6a | Strategy registry | — | — | ✅ (extraction step) | — | `strategies/` |
| 6b | LLM strategy selector | — | — | — | — | `strategy_selector.py` |

---

## V3: Harness Robustness (2026-03-22)

### Background

After completing optimization runs across `energy-mar21` and `nasdaq100-mar21` worktrees, post-run agent reflections identified structural weaknesses in the V2 harness. Full details, contradiction analysis, and verdicts are in `harness_upgrade_20260322_1403.md`.

**Root causes discovered:**

| Root cause | Effect | Addressed by |
|---|---|---|
| Look-ahead bias: indicators use today's close | Inflated backtest performance | R1 |
| Fixed-notional sizing: tight and wide stops get same position | Leverage artefact, not real alpha | R3 |
| 14-day test window (~10 trading days) | Too few trades to distinguish signal from noise | R2 (rolling windows) |
| Agent sees `test_pnl` every iteration | Implicit selection pressure toward test-window fits | R4 |
| Single training window | Optimizer exploits one regime, not structural edge | R2 |
| No per-trade data | Agent can only adjust global thresholds; flies blind | R5 |
| Sector concentration: 10+ correlated longs simultaneously | Leveraged macro bet, not diversified alpha | R8 |
| Knife-edge stop placement | Fragile to small fill deviations in live trading | R9 |

**Four contradictions identified** (see `harness_upgrade_20260322_1403.md` §Contradictions for full verdicts):

- **C1:** R4-simple vs R2 → R4's "print HIDDEN" option is dropped once R2 is implemented; use R4's 3-segment silent holdout instead
- **C2:** R2 vs R7 as primary objective → R2 (min test PnL) is primary; R7 (Calmar) is a printed diagnostic only
- **C3:** R9-date-shift vs R2 → R9's date-shift perturbation is dropped (redundant with R2's rolling windows); only R9's price/stop perturbation is kept
- **C4:** R5 vs R9 → When R9 is active, `trades.tsv` annotates `pnl_min`; a new `discard-fragile` status distinguishes fragility discards from genuine underperformance

---

### Implementation Plans

V3 is organized into sequential implementation plans, each executable as a single plan+execution session. **Run in order** — V3-B's walk-forward results are more meaningful after V3-A's sizing fix, V3-C layers on top of the new framework, and V3-E requires all prior changes to be present before modifying the walk-forward loop.

Every plan that touches the immutable zone requires a `GOLDEN_HASH` update in `tests/test_optimization.py`.

---

#### V3-A: Signal Correctness (R1, R3, R5, R4-partial)

**Goal:** Fix the measurement foundation before restructuring the evaluation loop. Every subsequent plan's results become more meaningful once these are in place.

**Why group these together:** All address "what does the optimizer actually measure." R3 and R5 are both additive changes to `run_backtest()` — one GOLDEN_HASH update covers both. R1 is mutable-zone only. R4-partial is a `program.md` instruction change with no code change.

| Rec | Description | Change |
|-----|-------------|--------|
| R1 | Fix look-ahead bias | In `screen_day()`: compute all indicators on `df.iloc[:-1]` (yesterday + prior); read only `price_10am` from `df.iloc[-1]` (today) for entry/stop checks |
| R3 | Risk-proportional sizing | Add `RISK_PER_TRADE = 50.0` to mutable constants; in `run_backtest()` replace `shares = 500.0 / entry_price` with `shares = RISK_PER_TRADE / (entry_price - signal['stop'])` |
| R5 | Trade-level attribution | In `run_backtest()`: accumulate per-trade records `{ticker, entry_date, exit_date, days_held, stop_type, entry_price, exit_price, pnl}`; write to `trades.tsv` from `__main__`. `screen_day` signal dict must include `stop_type: 'pivot' | 'fallback'` |
| R4 (partial) | Behavioral: don't act on test PnL | Update `program.md` keep/discard to use `train_total_pnl` only; document that `test_total_pnl` is printed but not used as a decision signal during the loop |

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable | R1: indicator slice; R3: `RISK_PER_TRADE` constant |
| `train.py` | Immutable | R3: shares formula; R5: trade accumulation + `trades.tsv` write; `screen_day` signal contract extended with `stop_type` |
| `program.md` | — | R4 partial: keep/discard instructions |
| `tests/test_optimization.py` | — | Update `GOLDEN_HASH` |

**Validation:**
- `trades.tsv` written after each run with correct columns
- Verify R3: wide ATR-fallback stops produce smaller share counts than tight pivot stops
- Verify R1: first entry date's indicators reflect yesterday's close, not today's

---

#### V3-B: Walk-Forward Evaluation Framework (R2, R4-full, R7)

**Goal:** Replace the single train→test split with rolling walk-forward CV as the optimization objective. This is the highest-impact single change for generalizability.

**Pre-requisite:** V3-A complete.

**Why group these together:** All restructure how `__main__` evaluates each experiment. R2 is the centerpiece; R4-full (3-segment silent holdout) is a natural companion since both define the evaluation horizon; R7 adds metrics to what's already being computed. One GOLDEN_HASH update.

**Contradiction handling applied:**
- C1: R4-simple dropped; R4-full's visible-test windows become the walk-forward folds for R2
- C2: R7 is a diagnostic column only; R2's `min_test_pnl` is the sole keep/discard criterion

| Rec | Description | Change |
|-----|-------------|--------|
| R2 | Walk-forward CV | Add `WALK_FORWARD_WINDOWS = 3` to mutable constants. `__main__` runs `run_backtest()` N times with staggered train/test windows (10-day step); reports one result block per window plus `min_test_pnl`. Keep/discard criterion: `min_test_pnl` |
| R4 (full) | Silent holdout | Add `SILENT_END` constant (set at setup: `TRAIN_END − 14 calendar days`). `__main__` runs an additional backtest for `TRAIN_END → BACKTEST_END`; suppresses its output during the loop (`silent_pnl: HIDDEN`); reveals in the final run only |
| R7 | Calmar + consistency | `run_backtest()` returns `max_drawdown` and per-month PnL breakdown. `print_results()` emits `train_calmar:` and `train_pnl_consistency:` (min monthly PnL). Added as columns to `results.tsv`; not used for keep/discard |

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable | R2: `WALK_FORWARD_WINDOWS` constant; R4-full: `SILENT_END` constant |
| `train.py` | Immutable | R2: rolling-window loop in `__main__`; `min_test_pnl` aggregation; R4-full: silent holdout call + output suppression; R7: `max_drawdown` + monthly PnL in `run_backtest()` return; new print lines in `print_results()` |
| `program.md` | — | Setup: compute `SILENT_END`; loop: keep/discard on `min_test_pnl`; results.tsv schema: add `min_test_pnl`, `train_calmar`, `train_pnl_consistency` columns |
| `tests/test_optimization.py` | — | Update `GOLDEN_HASH` |

**Validation:**
- Rolling windows produce distinct trade sets (spot-check with `grep "^train_backtest_start:" run.log`)
- `min_test_pnl` is correctly the minimum across all window test scores
- Silent holdout output absent from run.log during loop; present in final run
- Calmar and consistency columns appear in `results.tsv`

---

#### V3-C: Portfolio Robustness Controls (R8, R9-price-only)

**Goal:** Add optional controls that penalize concentration risk and fragile stop placement. Both are gated by mutable-section constants so the agent can tune them during optimization runs.

**Pre-requisite:** V3-A complete. V3-B strongly recommended (R9 cost: seeds × walk-forward windows — confirm still fast enough before enabling).

**Why group these together:** Both are optional additions to `run_backtest()` controlled by mutable constants. One GOLDEN_HASH update.

**Contradiction handling applied:**
- C3: R9's date-shift perturbation dropped (covered by V3-B's rolling windows); only price/stop perturbation implemented
- C4: When R9 active, `trades.tsv` annotated with `pnl_min`; new `discard-fragile` status added

| Rec | Description | Change |
|-----|-------------|--------|
| R8 | Position cap + correlation penalty | Add `MAX_SIMULTANEOUS_POSITIONS = 5` and `CORRELATION_PENALTY_WEIGHT = 0.0` to mutable constants. `run_backtest()` skips new entries when `len(portfolio) >= MAX_SIMULTANEOUS_POSITIONS`; subtracts `CORRELATION_PENALTY_WEIGHT × avg_pairwise_correlation × total_pnl` from reported metric |
| R9 (price only) | Robustness perturbation | Add `ROBUSTNESS_SEEDS = 0` to mutable constants (0 = off; 5 = enabled). When enabled, `run_backtest()` runs N seeds with entry prices ±0.5% and stop levels ±0.3 ATR; reports `pnl_min`. `trades.tsv` reflects nominal seed with header annotation `# pnl_min: $X.XX`. Status `discard-fragile` logged when nominal PnL > 0 but `pnl_min < 0` |

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable | R8: two constants; R9: `ROBUSTNESS_SEEDS` constant |
| `train.py` | Immutable | R8: portfolio cap guard + correlation penalty; R9: perturbation loop + `pnl_min` output line |
| `program.md` | — | Document `discard-fragile` status; instruct agent to inspect `trades.tsv` annotation when a fragility discard occurs |
| `tests/test_optimization.py` | — | Update `GOLDEN_HASH` |

**Validation:**
- With `MAX_SIMULTANEOUS_POSITIONS = 2` on a multi-ticker universe: verify no more than 2 positions ever open simultaneously
- With `ROBUSTNESS_SEEDS = 5`: verify `pnl_min` line appears in run.log; verify `trades.tsv` has annotation header
- `discard-fragile` rows appear in results.tsv when nominal PnL is positive but `pnl_min < 0`

---

#### V3-D: Diagnostics and Advanced (R6, R10, R11) — Future

Lower-priority improvements. Each can be scheduled independently as a small standalone plan.

| Rec | Description | Effort | Notes |
|-----|-------------|--------|-------|
| R10 | Bootstrap CI on final PnL | Low | Additive to `_write_final_outputs()` only. Can be added as a minor item to V3-A or V3-C. |
| R11 | Regime-conditional parameters | Low (partial) | `screen_day()` change only (mutable zone) — can be done by the agent during any optimization run. Harness-level per-regime stats require a future immutable-zone plan. |
| R6 | Ticker holdout | Medium | Requires `__main__` restructuring to hold out ticker subsets. Lower urgency once V3-B's walk-forward CV is in place. |

---

#### V3-E: Configurable Walk-Forward Window Size and Rolling Training Windows

**Pre-requisite:** V3-D complete (all V3-A through V3-D changes present in `train.py`).

**Goal:** Replace the two hardcoded `10`-business-day values in the walk-forward loop with configurable constants, and add support for rolling training windows. This makes test folds statistically meaningful (enough trades per fold to distinguish signal from noise) and allows training windows to span genuinely different market regimes.

**Motivation:**

The V3-B walk-forward loop hardcodes `10` business days for both the test window width and the step between folds. With a momentum screener on 85 tickers, a 10-business-day window yields only ~10–30 trades — too thin to distinguish a 60% win rate from luck (95% CI spans ~30 percentage points). Additionally, with `WALK_FORWARD_WINDOWS = 3`, all three test folds cluster within the most recent 6 weeks of the backtest window, all in the same market regime. `min_test_pnl` therefore measures "consistent in recent 6 weeks" rather than "resilient across diverse conditions."

Two separate problems require two separate fixes:

| Problem | Fix |
|---------|-----|
| Test folds too short — ~15 trades, wide CI, looks like noise | `FOLD_TEST_DAYS` constant — set to 20 (≈1 calendar month, ~40–100 trades on 85 tickers) |
| All folds in same recent regime — training windows nearly identical to each other | `FOLD_TRAIN_DAYS` constant — set to 0 (expanding) or e.g. 120 (6-month rolling); combined with more folds to spread test coverage across the full backtest window |

**New constants (mutable section of `train.py`):**

```python
# Test window width in business days per fold.
# 20 ≈ 1 calendar month → ~40–100 trades on 85 tickers; enough to distinguish skill from noise.
# Set at session setup. Do NOT change during the loop.
FOLD_TEST_DAYS = 20

# Training window width in business days.
# 0 = expanding: each fold trains from BACKTEST_START to its test window's start (all prior history).
# N > 0 = rolling: each fold trains on only the N most recent business days before its test window,
#   exposing successive folds to genuinely different market slices.
# Recommended: 0 (expanding) for simplicity; 120 (≈6 months) for maximum regime diversity.
# Set at session setup. Do NOT change during the loop.
FOLD_TRAIN_DAYS = 0
```

**Walk-forward loop change (immutable zone):**

Replace the two hardcoded `10` values and add rolling train window logic. Diff relative to V3-B/D:

```python
# Before (V3-B, hardcoded):
_fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * 10)
_fold_test_start_ts = _fold_test_end_ts - _BDay(10)
_fold_train_end_ts  = _fold_test_start_ts
_fold_train_stats = run_backtest(_train_ticker_dfs, start=BACKTEST_START, end=_fold_train_end)

# After (V3-E, configurable):
_fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * FOLD_TEST_DAYS)
_fold_test_start_ts = _fold_test_end_ts - _BDay(FOLD_TEST_DAYS)
_fold_train_end_ts  = _fold_test_start_ts

if FOLD_TRAIN_DAYS > 0:
    _fold_train_start_ts = _fold_train_end_ts - _BDay(FOLD_TRAIN_DAYS)
    _fold_train_start = str(max(_fold_train_start_ts.date(),
                                date.fromisoformat(BACKTEST_START)))
else:
    _fold_train_start = BACKTEST_START

_fold_train_stats = run_backtest(_train_ticker_dfs, start=_fold_train_start, end=_fold_train_end)
```

No other harness logic changes. `FOLD_TEST_DAYS` also replaces the `10` in the step calculation, so test windows remain non-overlapping regardless of the chosen size.

**Recommended configuration for the current 19-month window (2024-09-01 → 2026-03-20):**

```python
FOLD_TEST_DAYS      = 20   # 1 month per fold
FOLD_TRAIN_DAYS     = 0    # expanding: simpler, more training data per fold
WALK_FORWARD_WINDOWS = 9   # 9 × 20 days = 180 business days ≈ 9 months of test coverage
                           # test folds span ~Jun 2025 → Mar 2026
```

For maximum regime diversity (trades off some training data per fold for genuinely different training slices):

```python
FOLD_TEST_DAYS      = 20
FOLD_TRAIN_DAYS     = 120  # 6-month rolling train window
WALK_FORWARD_WINDOWS = 13  # covers the full 19-month window; ~26 backtest calls per iteration
```

**Performance note:** Each iteration now runs `WALK_FORWARD_WINDOWS × 2` backtest calls (train + test per fold). At 9 folds, this is 18 calls vs the current 6 — roughly 3× slower per iteration. With 85 tickers and a 6-month window per fold, each call completes in ~2–5 seconds, so a full iteration takes ~30–60 seconds. A 30-iteration session runs in ~15–30 minutes — still viable overnight. At 13 folds with rolling windows, ~26 calls per iteration, expect ~45–90 seconds per iteration.

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable | Add `FOLD_TEST_DAYS = 20` and `FOLD_TRAIN_DAYS = 0` constants near `WALK_FORWARD_WINDOWS` |
| `train.py` | Immutable | Walk-forward loop: replace 2 hardcoded `10` values with `FOLD_TEST_DAYS`; add `FOLD_TRAIN_DAYS` branch for rolling train window start |
| `program.md` | — | Document `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`, and updated `WALK_FORWARD_WINDOWS` recommendation; update setup step 4b to include these constants |
| `tests/test_optimization.py` | — | Update `GOLDEN_HASH` (immutable zone changed) |

**Backward compatibility:** Setting `FOLD_TEST_DAYS = 10` and `FOLD_TRAIN_DAYS = 0` exactly reproduces the V3-B/D walk-forward behavior. The default values in code are set to the new recommended values (`FOLD_TEST_DAYS = 20`); the agent sets them during session setup per `program.md`.

**Validation:**
- With `FOLD_TEST_DAYS = 10`, `FOLD_TRAIN_DAYS = 0`, `WALK_FORWARD_WINDOWS = 3`: output matches V3-D baseline (fold boundaries identical)
- With `FOLD_TEST_DAYS = 20`, `FOLD_TRAIN_DAYS = 0`, `WALK_FORWARD_WINDOWS = 9`: 9 fold result blocks print; oldest fold test window starts ~9 months before `TRAIN_END`
- With `FOLD_TRAIN_DAYS = 120`: fold 1 train start is `BACKTEST_START + 0` days (clamped); fold 9 train start is `FOLD_9_TEST_START - 120 business days`; verify no fold's train start precedes `BACKTEST_START`
- `GOLDEN_HASH` test passes after update

---

#### V3-F: Test-Universe Ticker Holdout and Per-Session Cache Path

**Pre-requisite:** V3-E complete.

**Goal:** Expose the walk-forward test folds to additional tickers that are never seen during training, so the optimization objective rewards strategies that generalize across the ticker universe rather than overfitting to the 85 training names. Also, make the cache path configurable per session so operators can maintain independent datasets for different experiments.

---

**Problem 1 — Test and train folds use identical ticker universe**

The walk-forward loop currently calls `run_backtest(_train_ticker_dfs, ...)` for both the training and test phases of every fold. The test fold therefore validates the strategy on exactly the tickers it was optimized for. Adding 2 unseen tickers per sector (~16 total) to the test folds would force `min_test_pnl` to measure "this signal works on tickers the agent has never adjusted parameters for", which is a significantly stronger generalization signal.

**Problem 2 — Single global cache path prevents independent experiments**

`CACHE_DIR` is hardcoded to `~/.cache/autoresearch/stock_data/`. All sessions share this directory. This is safe when sessions use the same date range (files are shared, no conflict), but creates silent data errors if two sessions need different history lengths for the same ticker — `prepare.py` skips existing files, so the second session silently uses stale data.

---

**Solution 1 — `TEST_EXTRA_TICKERS` mutable constant**

Add a new mutable constant listing tickers to include in test folds but not in training:

```python
# Tickers included in walk-forward TEST folds only — never in training.
# Used to measure out-of-universe generalization: min_test_pnl must hold on
# tickers the agent has never directly optimized for.
# These tickers must be downloaded by prepare.py before running.
# Set at session setup. Do NOT change during the loop.
TEST_EXTRA_TICKERS: list = []
```

In the immutable zone, after the train/holdout split, build a test-universe dict:

```python
_extra_ticker_dfs = {t: ticker_dfs[t] for t in TEST_EXTRA_TICKERS if t in ticker_dfs}
_test_ticker_dfs  = {**_train_ticker_dfs, **_extra_ticker_dfs}
```

Then in the fold loop, pass `_test_ticker_dfs` to the test call only:

```python
_fold_train_stats = run_backtest(_train_ticker_dfs, start=_fold_train_start, end=_fold_train_end)
_fold_test_stats  = run_backtest(_test_ticker_dfs,  start=_fold_test_start,  end=_fold_test_end)
```

The agent sees only `min_test_pnl` (an aggregated scalar) — it cannot observe per-ticker test breakdowns during the loop. The extra tickers therefore cannot be directly overfit to; they influence the objective only through the aggregate.

**Interaction with R6 ticker holdout (`TICKER_HOLDOUT_FRAC`):** The existing R6 holdout splits the base training universe by a fraction of sorted tickers and evaluates them on the full training time range. `TEST_EXTRA_TICKERS` is orthogonal — it adds tickers to the test time range, not the training time range. When using `TEST_EXTRA_TICKERS`, set `TICKER_HOLDOUT_FRAC = 0` to avoid confusion between the two mechanisms.

**Suggested extra tickers (2 per sector, not in the current 85-ticker training universe):**

| Sector | Extra tickers |
|--------|--------------|
| Tech | INTC, CSCO |
| Financials | TFC, USB |
| Healthcare | BMY, CVS |
| Energy | PSX, HES |
| Consumer Staples | MDLZ, SYY |
| Industrials | EMR, ITW |
| Consumer Discretionary | DG, YUM |
| Materials | DD, PKG |

Download these by adding them to `TICKERS` in `prepare.py` before running `prepare.py`. Once cached, move them out of `TICKERS` and into `TEST_EXTRA_TICKERS` in `train.py` (they must remain in `prepare.py`'s `TICKERS` list so re-runs of `prepare.py` keep them fresh).

---

**Solution 2 — Per-session `CACHE_DIR`**

Change `CACHE_DIR` from a hardcoded constant to one that reads an environment variable with a fallback to the current default:

```python
# Cache directory for parquet files. Override with AUTORESEARCH_CACHE_DIR env var
# to maintain independent datasets for different sessions or date ranges.
CACHE_DIR = os.environ.get(
    "AUTORESEARCH_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "stock_data")
)
```

Apply the same change to `prepare.py`. Sessions with a non-default cache path set the env var before running:

```bash
export AUTORESEARCH_CACHE_DIR=~/.cache/autoresearch/stock_data_alt
uv run prepare.py   # downloads to the alternate cache
uv run train.py     # reads from the alternate cache
```

Default behavior is identical to the current hardcoded path — no breaking change.

**When this matters:** Two sessions using overlapping tickers but different date ranges (e.g. one trained on 2024-09 → 2026-03, another on 2023-01 → 2025-01) must use separate cache dirs. Sessions with non-overlapping ticker universes or identical date ranges can safely share the default cache.

---

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable | Add `TEST_EXTRA_TICKERS: list = []` constant |
| `train.py` | Mutable | Change `CACHE_DIR` to env-var-with-fallback pattern |
| `train.py` | Immutable | Build `_extra_ticker_dfs` and `_test_ticker_dfs` after train/holdout split; pass `_test_ticker_dfs` to `_fold_test_stats` call |
| `prepare.py` | User config | Change `CACHE_DIR` to env-var-with-fallback pattern; add suggested extra tickers to `TICKERS` list |
| `program.md` | — | Document `TEST_EXTRA_TICKERS` setup and `AUTORESEARCH_CACHE_DIR` env var in session setup steps |
| `tests/test_optimization.py` | — | Update `GOLDEN_HASH` (immutable zone changes) |

**Backward compatibility:** `TEST_EXTRA_TICKERS = []` (default) leaves `_test_ticker_dfs == _train_ticker_dfs`, reproducing current behavior exactly. `CACHE_DIR` fallback is the current hardcoded path, so existing sessions require no changes.

**Validation:**
- With `TEST_EXTRA_TICKERS = []`: fold test P&L identical to V3-E baseline (same tickers)
- With `TEST_EXTRA_TICKERS = ["INTC", "CSCO"]`: fold test output includes trades from INTC/CSCO; training output does not
- `AUTORESEARCH_CACHE_DIR=/tmp/test_cache uv run prepare.py` writes to `/tmp/test_cache/`; `uv run train.py` with same env var reads from there

---

#### V3-G: Harness Integrity and Objective Quality

**Pre-requisite:** V3-F complete.

**Goal:** Prevent the agent from gaming the `min_test_pnl` objective through position-sizing inflation, add a monthly P&L floor to the keep/discard criterion, add an early-stop signal for zero-trade plateaus, restructure the mutable section so session-setup constants are structurally separated from strategy-tuning constants, and make ticker holdout the recommended default.

---

**Background**

Post-run analysis from a pre-V3-E session identified four remaining harness weaknesses not addressed by V3-A through V3-F:

| # | Weakness | Addressed by |
|---|----------|-------------|
| 1 | `RISK_PER_TRADE` is in the strategy-tunable zone — agent can inflate all P&L by raising it | Mutable-section restructure + reinforced program.md |
| 4 | No signal when screener plateaus at zero trades — agent loops indefinitely with no feedback | `program.md` early-stop instruction |
| 5 | `pnl_consistency` (min monthly PnL) is printed but not enforced — agent can keep strategies with deeply negative monthly drawdowns | `program.md` keep/discard guard |
| 6 | `TICKER_HOLDOUT_FRAC = 0.0` default means holdout is never used unless the session operator enables it explicitly | `program.md` session-setup recommendation |

---

**Problem 1 — RISK_PER_TRADE is agent-tunable**

`RISK_PER_TRADE` lives in the mutable section alongside `MAX_SIMULTANEOUS_POSITIONS` and other constants the agent is expected to tune. An agent that raises `RISK_PER_TRADE` from 50 to 500 produces 10× the dollar P&L for the same strategy with no genuine improvement. This is the highest-priority integrity issue.

Broader observation: the mutable section currently mixes two conceptually different constant types with no structural distinction:

- **Session-setup constants** — set once at the start of a session by the session operator; must not change during the experiment loop (`BACKTEST_START`, `TRAIN_END`, `RISK_PER_TRADE`, `FOLD_TEST_DAYS`, `TICKER_HOLDOUT_FRAC`, etc.)
- **Strategy-tuning constants** — the agent is explicitly expected to modify these during experiments (`MAX_SIMULTANEOUS_POSITIONS`, `CORRELATION_PENALTY_WEIGHT`, `ROBUSTNESS_SEEDS`)

Without a visible boundary, the agent treats the entire mutable section as fair game.

**Solution:** Add clear comment headers to sub-divide the mutable section into two labeled zones:

```python
# ══ SESSION SETUP — set once at session start; DO NOT change during experiments ══════
# These constants define the evaluation framework. Changing them mid-session
# invalidates comparisons across experiments.
BACKTEST_START = ...
BACKTEST_END   = ...
TRAIN_END      = ...
...
RISK_PER_TRADE = 50.0       # dollar risk per trade; DO NOT raise to inflate P&L
FOLD_TEST_DAYS = 20
...
TICKER_HOLDOUT_FRAC = 0.0

# ══ STRATEGY TUNING — agent may modify these freely during experiments ════════════════
# Only the constants below this line are valid optimization targets.
MAX_SIMULTANEOUS_POSITIONS = 5
CORRELATION_PENALTY_WEIGHT = 0.0
ROBUSTNESS_SEEDS = 0
```

This is a mutable-zone-only change — no `GOLDEN_HASH` update required.

`program.md` must also be updated: "Only modify constants in the STRATEGY TUNING sub-section during experiments. Never modify SESSION SETUP constants — doing so invalidates cross-experiment comparisons."

---

**Problem 4 — No early-stop when screener produces zero trades**

If screener thresholds are tightened to the point that no signals fire across the entire training window, `total_trades: 0` is returned and `min_test_pnl = 0.0`. The agent may not recognize this as a dead-end and continue iterating with no informative signal.

**Solution:** `program.md` instruction addition:

> If `train_total_trades: 0` appears for 3 or more consecutive iterations, stop tuning screener thresholds in that direction. Relax the most recently tightened threshold back to its prior value and try a different modification. If 10 consecutive iterations all produce zero trades, log status `plateau` and stop the session; notify the user.

This is a `program.md`-only change. No code changes required.

---

**Problem 5 — `pnl_consistency` not enforced in keep/discard**

`pnl_consistency` (minimum monthly P&L, computed by `run_backtest()` since V3-B R7) is printed as a diagnostic but not used in the keep/discard decision. A strategy can achieve high `min_test_pnl` by making most of its P&L in one month while losing money in others — a pattern that is not robust.

**Solution:** Add a secondary keep condition to `program.md`:

> Keep a change only if **both** conditions hold:
> 1. `min_test_pnl` improved (higher than current best)
> 2. `train_pnl_consistency` ≥ `−RISK_PER_TRADE × 2` (minimum monthly P&L is not catastrophically negative)
>
> If `min_test_pnl` improved but `train_pnl_consistency` is below the floor, log status `discard-inconsistent` and revert.

The threshold `−RISK_PER_TRADE × 2` scales with position sizing, avoiding a hardcoded dollar amount.

This is a `program.md`-only change. The `results.tsv` schema should add a `train_pnl_consistency` column (the value is already printed in `run.log`).

---

**Problem 6 — Ticker holdout disabled by default**

`TICKER_HOLDOUT_FRAC = 0.0` is the code default, and the session setup instructions do not recommend a non-zero value. As a result, the holdout mechanism added in V3-D is never activated unless the operator explicitly enables it. This leaves the most obvious generalization guard unused by default.

**Solution:** Update `program.md` session setup step 4b:

> Set `TICKER_HOLDOUT_FRAC = 0.1` as the recommended starting value (hold out the alphabetically last 10% of tickers as a silent training-universe holdout). Only set to `0.0` if the ticker universe is small (< 20 tickers) or if `TEST_EXTRA_TICKERS` is in use.

This is a `program.md`-only change. No code changes required.

---

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable | Add `# ══ SESSION SETUP ══` and `# ══ STRATEGY TUNING ══` sub-section headers; add explicit "DO NOT raise to inflate P&L" comment to `RISK_PER_TRADE` |
| `program.md` | — | (1) "only modify STRATEGY TUNING sub-section during experiments"; (2) early-stop on zero-trade plateau; (3) `train_pnl_consistency` floor in keep/discard; (4) `TICKER_HOLDOUT_FRAC = 0.1` recommended default; (5) add `train_pnl_consistency` column to `results.tsv` schema |

**No immutable-zone changes** — no `GOLDEN_HASH` update required.

**Backward compatibility:** All changes are additive comments or `program.md` instructions. Existing worktrees and cached data are unaffected.

**Validation:**
- `python -c "import train"` — no errors
- Confirm `# ══ SESSION SETUP ══` and `# ══ STRATEGY TUNING ══` headers present in `train.py` mutable section
- `program.md` contains "plateau", "discard-inconsistent", "TICKER_HOLDOUT_FRAC = 0.1"

---

### V3 Compatibility Notes

- V3-A, V3-B, V3-C, V3-E, V3-F each touch the immutable zone → each requires a `GOLDEN_HASH` update in `tests/test_optimization.py`
- V3-G touches the mutable zone and `program.md` only → **no `GOLDEN_HASH` update required**
- **Sequence dependency:** V3-A → V3-B → V3-C → V3-D → V3-E → V3-F → V3-G. V3-G must follow V3-F (all prior harness changes present).
- After V3-G: only constants in the `# ══ STRATEGY TUNING ══` sub-section are valid agent optimization targets
- The `strategies/` registry (V2 Enhancement 6) and `scripts/fetch_all_strategies.py` are unaffected by V3
- **Out-of-scope items from V1 that V3 now addresses:** "Max concurrent positions cap" → R8; "Walk-forward validation" → R2; "Risk-per-trade sizing" → R3

---

## V4: Signal Quality and Measurement Fidelity (2026-03-23)

### Background

After completing the first wide-universe optimization run (`multisector-mar23`: 85 tickers, 9 GICS sectors, 19 months, 30 iterations), post-run analysis of `trades.tsv` and the fold-by-fold optimization history revealed two categories of structural problems. Full details are in `harness_upgrade_20260323_2249.md`.

**Root causes discovered:**

| Root cause | Effect | Addressed by |
|---|---|---|
| Fallback-stop entries (no structural support below price) | 25% win rate vs 79% for pivot-stop entries; −$135 net drag | R9 |
| Earnings-proximity entries (within 14 days of next release) | 6 of 10 losses are earnings-day stop-hits; −$300 attributable | R8 |
| FOLD_TEST_DAYS=20 too short for 30–98 day hold duration | Fold trade counts of 0–2; min_test_pnl measures entry quality, not position outcome | R1 |
| Consistency floor hard-coded at −$100 (designed for small universes) | Genuine improvements tagged `discard-inconsistent`; wasted 6+ iterations | R3 |
| min_test_pnl dominated by single trade when fold trade count < 3 | Optimization locked on unimprovable single-event outcomes for 17+ iterations | R2 |
| Stalled positions hold slots for 88–98 days earning < $13 each | Capital inefficiency; best returns are in the 15–30 day cohort | R10 |
| Deadlock: worst fold unchanged for 4+ consecutive iterations | Budget exhausted on one fold; other folds not improved | R5 |
| Position management tested last, not early | Trail trigger and breakeven changes found at iter 27–28 out of 30 | R6 |
| avg_win ($21) / avg_loss ($49) = 0.43× not tracked | Silent erosion of profitability ratio accepted by keep/discard loop | R11 |
| avg_pnl_per_trade and win_rate not in primary output | Trade-count-dependent metric (`train_total_pnl`) is the only training signal | R4 |
| Sector concentration not guarded at portfolio level | Multiple simultaneous positions in same sector amplify single-sector events | R7 |
| Re-entries in recently stopped tickers ("dead-cat bounce") | UNH −$100 total, HD −$50 on re-entry, FCX −$43 on re-entry | R12 |

**One contradiction identified:**

- **C1: R9 vs R12** — R9 rejects fallback-stop entries; R12 rejects re-entries in recently stopped tickers. Three trades are blocked by both rules simultaneously (RTX), making it impossible to isolate causation if implemented together. **Verdict:** Apply R9 first (one-line, high-certainty gain). Implement R12 in a separate subsequent run and measure the incremental effect independently.

---

### Implementation Plans

V4 is organized into three execution runs. **V4-A and V4-B are both prerequisites for Phase 1 sector worktrees** (see `after_baseline.md`). **V4-C must run after Phase 1 is complete** — it introduces a screener signature change that requires validation against sector-specific trade histories before being trusted.

V4-A has no GOLDEN_HASH dependency (all changes are above the `DO NOT EDIT` boundary or in `program.md`/`prepare.py`). V4-B touches the immutable zone and requires a single GOLDEN_HASH update covering all three recommendations.

---

#### V4-A: Strategy Quality and Loop Control (R8, R9, R10, R1, R3, R5, R6)

**Goal:** Fix the sources of systematic loss that no amount of screener iteration can address, and improve the agent loop instructions so the next 30 iterations are spent productively rather than deadlocked.

**Pre-requisite:** Baseline strategy copied to master `train.py` (Phase 0 Step 2, already done).

**Why group these together:** None touch the immutable zone. R8 and R9 both modify `screen_day()` and should be in the same diff to avoid boundary-crossing confusion. R10 modifies `manage_position()`. R1, R3, R5, R6 are `program.md`-only changes with no code risk. No GOLDEN_HASH update required for this run.

| Rec | Description | Change |
|-----|-------------|--------|
| R9 | Reject fallback-stop entries | In `screen_day()`: change fallback branch (`stop = None`) from assigning `entry − 2.0×ATR` to `return None`. One line. Removes trades with no structural support below entry. |
| R8 | Earnings-proximity filter | In `prepare.py`: add `next_earnings_date` column to each ticker's parquet using `yf.Ticker(t).earnings_dates` (covers ~4 years). In `screen_day()`: add guard `if 0 <= (next_earnings_date − today).days <= 14: return None` before all other checks. Re-run `prepare.py` to refresh parquet files after the code change. |
| R10 | Time-based capital-efficiency exit | In `manage_position()`: if position held > 30 business days AND current unrealised P&L < 0.3 × RISK_PER_TRADE, return stop at current `price_10am` to force exit. Preserves APP (49d/+$81) and PLTR (72d/+$40); recycles COF (89d/+$7) and AMZN (98d/+$13). |
| R1 | Widen FOLD_TEST_DAYS | In `program.md` setup instructions: change recommended `FOLD_TEST_DAYS` default from 20 to 40. Add note: reduce `WALK_FORWARD_WINDOWS` to 7 if the total date range cannot fit 9 folds of 40 days each. |
| R3 | Auto-calibrate consistency floor | In `program.md` keep/discard instructions: replace `−RISK_PER_TRADE × 2` with `−RISK_PER_TRADE × MAX_SIMULTANEOUS_POSITIONS × 10`. Add a note that this scales with universe size. |
| R5 | Deadlock detection pivot | In `program.md` loop instructions: add rule — if `min_test_pnl` has not changed for 4 consecutive kept iterations, pivot to optimizing `mean_test_pnl` for the next 3 iterations before returning to `min_test_pnl`. |
| R6 | Test position management early | In `program.md` loop instructions: add explicit guidance to test trailing-stop distance, breakeven trigger, and stop distance in iterations 6–10, not only after screener ideas are exhausted. |

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `prepare.py` | — | R8: fetch and store `next_earnings_date` per ticker |
| `train.py` | Mutable (above boundary) | R8: earnings guard in `screen_day()`; R9: fallback-stop reject in `screen_day()`; R10: time-based exit in `manage_position()` |
| `program.md` | — | R1: FOLD_TEST_DAYS default; R3: consistency floor formula; R5: deadlock pivot rule; R6: loop iteration order |

**Validation:**
- `python -c "import train"` — no errors
- `screen_day()` returns None for a synthetic ticker with `next_earnings_date` within 14 days
- `screen_day()` returns None for a synthetic signal where `find_stop_price()` returns None
- `manage_position()` returns a stop at current price when days_held > 30 and unrealised PnL < $15
- `prepare.py` writes `next_earnings_date` column to at least one parquet file; column is readable as a date in `screen_day()`
- Full test suite passes; GOLDEN_HASH test passes (no immutable zone was touched)

---

#### V4-B: Harness Metric Improvements + Position Management Refinements (R2, R4, R11, R7, R13, R14, R15)

**Goal:** Fix what the optimization loop measures and tracks (R2, R4, R11, R7), and apply three position management refinements discovered from multisector-mar23 trade analysis (R13, R14, R15). The fold-floor deadlock (R2), the absence of per-trade quality metrics (R4, R11), and the unguarded sector concentration (R7) all silently accept bad strategy states. R13–R15 tighten exit mechanics to capture more of each winner and limit early stalls.

**Pre-requisite:** V4-A complete.

**Why group these together:** R2, R4, R11, R7 all touch the immutable zone (`run_backtest()`, `print_results()`, or `__main__`) and require one GOLDEN_HASH update. R13, R14, R15 are purely mutable zone (`manage_position()`) — no GOLDEN_HASH impact — but are small enough to include in the same execution run. R13 and R15 are one-line changes; R14 is the most complex (partial position close requires harness coordination, see note below).

| Rec | Description | Change |
|-----|-------------|--------|
| R2 | min_test_pnl fold trade-count guard | In the walk-forward fold aggregation (immutable `__main__`): when computing `min_test_pnl`, exclude any fold whose test-window trade count is < 3. If all folds are excluded, fall back to the raw minimum. Print `min_test_pnl_folds_included: N` alongside `min_test_pnl`. |
| R4 | Add avg_pnl_per_trade and win_rate to output | In `print_results()`: emit `train_avg_pnl_per_trade:` and `train_win_rate:` lines. Add both as columns to `results.tsv`. These are already computable from existing `run_backtest()` return values; no backtest logic changes required. |
| R11 | Track win/loss dollar ratio | In `run_backtest()`: compute `avg_win_loss_ratio = mean(winning_trade_pnls) / abs(mean(losing_trade_pnls))`; return in stats dict. In `print_results()`: emit `train_win_loss_ratio:`. Add to `results.tsv`. In `program.md`: add a soft guard — if `avg_win_loss_ratio < 0.5` and `train_pnl_consistency < floor`, flag as `discard-fragile`. |
| R7 | Sector concentration guard *(optional, low priority)* | In `run_backtest()`: add `MAX_POSITIONS_PER_SECTOR` constant (default: large int = disabled). Before opening a new position, count open positions in the same GICS sector; skip entry if count ≥ cap. Sector is read from a `sector` field in the `screen_day()` signal dict, which is derived from the ticker's parquet metadata or a static lookup. |
| R13 | Tighten trailing stop distance 1.5× → 1.2× ATR | In `manage_position()`: change `round(recent_high - 1.5 * atr, 2)` to `round(recent_high - 1.2 * atr, 2)`. Simulation across 27 winning trades in multisector-mar23 shows zero early exits at 1.2×; the only winner clipped at 1.0× is APP Sep 2025 by $10.49, making 1.2× the safe floor. Locks in ~$3 more per exit on a $500 stock with $10 ATR without cutting any meaningful winners. |
| R14 | Partial profit taking at +1R *(moderate complexity)* | In `manage_position()` and `run_backtest()`: when `price_10am >= entry_price + 1.0 * atr` for the first time, close 50% of the position at current price and halve `position['shares']`. The remaining half continues with the trailing stop. Converts the all-or-nothing payoff structure (avg win $21 vs avg loss $49) into a two-part payoff with a $25 floor on the first partial. **Note:** requires `run_backtest()` to support partial closes — record a partial trade exit row at the half-close price and continue tracking the remainder. If partial closes are not feasible, implement as a tighter profit target (exit 100% at +1.5R) instead. |
| R15 | Early stall exit — no momentum after 5 days | In `manage_position()`, before the trailing stop logic: if `(current_date - entry_date).days <= 5` and `price_10am < entry_price + 0.5 * atr`, return a tightened stop at `price_10am` (or `entry_price - 0.3 * atr`) to force exit the next day. Targets the worst bucket (6–14 day, 5W/5L, −$162 net): SCHW 7d, UNH Oct 6d, RTX 0d, ORCL 1d were all stalling at day 5. Trades that move immediately (TSLA 8d, MSTR 2d, GOOGL 8d) clear the 0.5 ATR threshold within days 1–3 and are unaffected. Test with 3-day and 5-day windows and 0.3/0.5 ATR thresholds. |

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable | R7 (if implemented): `MAX_POSITIONS_PER_SECTOR` constant; R13: trail distance 1.5×→1.2× in `manage_position()`; R14: partial-exit logic in `manage_position()`; R15: early stall guard in `manage_position()` |
| `train.py` | Immutable | R2: fold exclusion guard in `__main__`; R4: new print lines + stats dict fields; R11: win/loss ratio computation + print line; R7: sector cap check in position-open logic; R14 (if partial closes needed): partial trade recording in `run_backtest()` |
| `program.md` | — | R4: update results.tsv schema; R11: `discard-fragile` condition; R2: note on `min_test_pnl_folds_included` interpretation |
| `tests/test_backtester.py` | — | R13/R15: update `manage_position` tests for new stop distances; R14: new partial-close tests |
| `tests/test_optimization.py` | — | Update `GOLDEN_HASH` |

**Validation:**
- `min_test_pnl_folds_included:` appears in run output; value is ≤ `WALK_FORWARD_WINDOWS`
- `train_avg_pnl_per_trade:` and `train_win_rate:` appear in run output and are parseable floats
- `train_win_loss_ratio:` appears in run output; value is positive
- All new columns present in `results.tsv` header
- R13: `manage_position()` returns `recent_high - 1.2 * atr` (not 1.5) once trailing is active
- R15: `manage_position()` returns a tightened stop when `days_held <= 5` and `price_10am < entry_price + 0.5 * atr`
- R14: partial trade row recorded in `trades.tsv` when price reaches +1R
- GOLDEN_HASH test passes after update
- Full test suite passes

---

#### V4-C: Post-Phase-1 — Recent Loser Cooldown (R12)

**Pre-requisite:** Phase 1 sector worktrees complete (see `after_baseline.md` Steps 5–7). R12 must be validated against multiple sector trade histories before being trusted — a single-sector trade history is too thin to confirm the 90-day lookback window is correct.

**Why after Phase 1:** R12 requires threading a `recently_stopped` set through the backtest loop into `screen_day()`, changing the function signature. This is the most invasive harness change in V4. Running Phase 1 first produces 11 independent sector trade histories that reveal whether repeat-loser re-entries are a systematic pattern (as in the baseline) or an artefact of the wide-universe setup. If the pattern does not appear in sector runs, R12 may not be necessary. If it does, the right cooldown window (60/90/120 days) can be calibrated from 11 data points rather than one.

**Also evaluate at this point:**
- Whether R8 (earnings filter) eliminated the pattern entirely — UNH Oct is both an earnings-day entry and a repeat loser. If R8 removes all cases that R12 would have caught, R12 may be redundant.
- Whether R9 (fallback-stop reject) already eliminated the RTX re-entry case.

| Rec | Description | Change |
|-----|-------------|--------|
| R12 | Recent loser cooldown | In `run_backtest()` (immutable): maintain a `recently_stopped: set[str]` of tickers that hit a full −1R stop (exit at initial stop, not trailing) within the last N calendar days (try N=60, 90, 120). Pass `recently_stopped` into each `screen_day()` call. In `screen_day()` (mutable): add `if ticker in recently_stopped: return None` as the first guard. **Note:** this changes `screen_day()`'s signature from `(df, today)` to `(df, today, recently_stopped=frozenset())`. All test fixtures and callers must be updated. |

**Files changed:**

| File | Zone | Changes |
|------|------|---------|
| `train.py` | Mutable (above boundary) | R12: `recently_stopped` parameter + guard in `screen_day()` |
| `train.py` | Immutable | R12: `recently_stopped` set maintenance in `run_backtest()`; pass-through to `screen_day()` |
| `tests/test_screener.py` | — | Update all `screen_day(df, today)` calls to include `recently_stopped` |
| `tests/test_backtester.py` | — | Update mock patches and direct calls |
| `tests/test_optimization.py` | — | Update `GOLDEN_HASH` |
| `program.md` | — | Document the `recently_stopped` mechanism; recommend N=90 as default |

**Validation:**
- `screen_day()` returns None when called with `recently_stopped={'AAPL'}` and ticker is AAPL
- `screen_day()` proceeds normally when `recently_stopped=frozenset()`
- Trade history from at least one sector run confirms re-entry losses are not eliminated by R8/R9 alone
- Full test suite passes; GOLDEN_HASH test passes after update

---

### V4 Compatibility Notes

- **V4-A has no GOLDEN_HASH dependency** — all changes are in `screen_day()`, `manage_position()`, `prepare.py`, or `program.md`. The immutable zone is untouched.
- **V4-B requires one GOLDEN_HASH update** covering R2, R4, R11, and optionally R7. R13, R14, R15 are mutable-zone changes bundled in the same run but do not affect the hash. Do all immutable-zone changes (R2, R4, R11, R7) in a single commit.
- **V4-C requires a GOLDEN_HASH update** and changes `screen_day()`'s public signature. Run after Phase 1 validates the approach.
- **Sequence dependency:** V4-A → V4-B → [Phase 1 from `after_baseline.md`] → V4-C. V4-A and V4-B are both prerequisites for Phase 1.
- **R8 requires a data refresh:** after updating `prepare.py`, re-run `uv run prepare.py` for all tickers before the next optimization run. The `next_earnings_date` column will be absent from existing parquet files until this is done.
- **R10 interacts with R12:** the time-based exit (R10) will reduce the number of long-held positions that accumulate into "stalled" entries. When calibrating R12's cooldown window in V4-C, verify that R10 is already active so its effect is not double-counted.
- **Out-of-scope items from V1 now addressed:** "Multiple simultaneous entries in the same ticker" → partially addressed by R12's cooldown; "Portfolio-level risk limits" → R7 (sector cap); "Transaction costs" → remains out of scope.

---

## V5: Instrumentation and Data Correctness (2026-03-24)

**Context:** After 27 optimization iterations on the global-mar24 worktree, all metrics are
based on incorrect data (9:30 AM open price instead of ~10:30 AM price) and the trade log
lacks the columns needed to diagnose exit quality. V5 fixes both before any further experiments.

**Plan file:** `.agents/plans/p0-price-fix-and-instrumentation.md`

### V5-A: Price Fix and Trade Instrumentation

Four tightly-coupled changes that must ship together before the next optimization run.

**Affected files:** `prepare.py`, `train.py` (mutable and immutable zones), `tests/test_prepare.py`,
`tests/test_optimization.py`, `tests/test_e2e.py`, `tests/test_v4_a.py`, `tests/test_v4_b.py`,
`tests/test_backtester.py`

#### P0-A: Fix price extraction (Close not Open) + column rename

`prepare.py` currently extracts the **Open** of the 9:30 AM hourly bar as `price_10am` — the
market-open price, the most volatile moment of the trading day. The strategy's premise of
waiting for post-open volatility to settle is defeated by using the open price.

**Fix:** Switch to the **Close** of the 9:30 AM bar (~10:30 AM, post-opening-volatility).
Rename `price_10am` → `price_1030am` throughout `prepare.py`, `train.py`, and all test fixtures.

```python
# prepare.py — BEFORE:
df_10am = df[mask][["Open"]].copy()
price_10am_series = df_10am["Open"].rename("price_10am")

# prepare.py — AFTER:
df_10am = df[mask][["Close"]].copy()
price_1030am_series = df_10am["Close"].rename("price_1030am")
```

**Impact:** All 27 prior optimization iterations used the wrong price. Re-running iter21
(`6ad6edd`) with the corrected price establishes a new baseline. The old parquet cache
(`stock_data/*.parquet`) is semantically invalid — must be deleted and regenerated via
`prepare.py` before the next optimization run.

**GOLDEN_HASH:** The rename in the immutable zone (`run_backtest()`, `_write_trades_tsv()`)
requires a GOLDEN_HASH update as the final step of V5-A.

#### P0-B: MFE/MAE columns in trades.tsv

Track per-trade Maximum Favorable Excursion and Maximum Adverse Excursion in ATR units:
- `mfe_atr = (max_price_seen − entry_price) / ATR14`
- `mae_atr = (entry_price − min_price_seen) / ATR14`

Implementation: add `high_since_entry` and `low_since_entry` fields to the open position dict.
Update them on each mark-to-market step. Compute MFE/MAE at close via `_mfe_mae(pos)` helper.

ATR units (not raw %) make MFE/MAE directly comparable to stop and trail thresholds already
expressed in ATR. Without this, it is impossible to diagnose whether exit loosening would help.

#### P0-C: Exit-type tagging

Add `exit_type` column to all trade records with values: `stop_hit`, `end_of_backtest`,
`partial`. (The `partial` exit type is already present from V4-B; P0-C ensures the other
two paths are also tagged consistently.)

#### P0-D: R-multiple

```
r_multiple = (exit_price − entry_price) / (entry_price − initial_stop)
```

Log at exit for every trade. Store `initial_stop` in the position dict at entry time. If
initial risk is zero, write empty string. A healthy momentum strategy shows winners clustering
at +1.5–3.0R and losers at -1.0R. A mass near 0R indicates the breakeven trigger is the
primary W/L suppressor.

#### P0-E: Volume criterion redesign ✅ Done (2026-03-25)

The backtested `vol_ratio` rule (`today_vol / vm30 >= 2.5`) uses today's partial volume,
which is 0 pre-market and only ~15–20% of daily volume at 10:30am — a genuine 1.9× day
registers as ~0.3× at entry time. The rule structurally eliminates all pre-market candidates
and misses real breakout days.

**Fix:** remove `today_vol` from `screen_day()` entry logic. Replace with two prior-data-only
rules: 5-day avg volume ≥ MA30 (Rule 3a) and yesterday's volume ≥ 0.8× MA30 (Rule 3b).
Return dict gains `prev_vol_ratio` and `vol_trend_ratio` (replacing `vol_ratio`).

Applied before Phase 0.2 re-calibration (2026-03-25) so the corrected volume rules are
baked into the new baseline from the start.

### V5-A Acceptance Criteria

- [ ] `prepare.py:resample_to_daily()` uses `Close` of 9:30 AM bar; column named `price_1030am`
- [ ] Zero occurrences of `"price_10am"` in `prepare.py`, `train.py`, `tests/test_prepare.py`,
  `tests/test_optimization.py`, `tests/test_e2e.py`
- [ ] `trades.tsv` schema includes `exit_type`, `mfe_atr`, `mae_atr`, `r_multiple` columns
- [ ] All trade records have a non-empty `exit_type` in `{"stop_hit", "end_of_backtest", "partial"}`
- [ ] MFE ≥ 0 and MAE ≥ 0 for all records; R-multiple sign is correct (positive for winners)
- [ ] `test_harness_below_marker_matches_golden_hash` passes with new GOLDEN_HASH
- [ ] Full test suite passes with 0 new failures (`python -m pytest tests/ -q`)
- [ ] PROGRESS.md updated with cache invalidation action note

### V5-B: Dedicated Test Parquet Fixture (2026-03-25)

Self-contained test dataset so integration and E2E tests run reliably in CI and fresh-clone
environments without a pre-populated 389-ticker cache.

**Changes:**
- `tests/conftest.py` — session-scoped `test_parquet_fixtures` downloads AAPL/MSFT/NVDA via
  yfinance on first run, caches to `~/.cache/autoresearch/test_fixtures/`. History: 2024-04-01
  (yfinance 1h 730-day limit); backtest window: 2024-09-01..2025-11-01.
- `train._compute_fold_params()` — auto-detects short backtest windows (< 130 bdays → 1 fold)
  so the harness doesn't crash with a tiny dataset.
- `tests/test_e2e.py` — all 9 `@pytest.mark.integration` tests wired to fixture; subprocess
  tests inject `AUTORESEARCH_CACHE_DIR` instead of using the full cache.
- `tests/test_optimization.py` — 3 `@_live` tests (formerly skipped without 389-ticker cache)
  converted to `@pytest.mark.integration` using the small fixture.
- GOLDEN_HASH updated for `_compute_fold_params` call in `__main__`.

**Acceptance criteria:**
- [ ] `pytest tests/test_fold_auto_detect.py` → 13 passed
- [ ] `pytest tests/test_e2e.py -m integration` → 9 passed, 0 skipped
- [ ] `pytest tests/test_optimization.py -m integration` → 3 passed (no longer skip)
- [ ] Full suite (`pytest tests/ --ignore=tests/test_selector.py`) → no new failures

### V5 Compatibility Notes

- **V5-A requires cache invalidation:** After updating `prepare.py`, delete all
  `stock_data/*.parquet` files and re-run `prepare.py`. Old parquet files contain the
  incorrect `price_10am` column (9:30 AM open — market open spike). New files will contain
  `price_1030am` (9:30 bar close — post-open-volatility price).
- **All prior iteration metrics are invalidated:** The 27 global-mar24 iterations and the
  iter21 baseline were evaluated on the wrong price. Treat pre-V5 metrics as approximate.
  Establish a new clean baseline after V5-A before running further exit experiments.
- **GOLDEN_HASH update is mandatory and must be last:** All immutable-zone changes (rename +
  P0-B/C/D) must be complete before recomputing the hash. The hash covers everything below
  the `# ── DO NOT EDIT BELOW THIS LINE` marker in `train.py`.
- **Sequence dependency:** V5-A must complete before Phase 0 exit experiments (phases.md).

---

## V6: Signal Quality, Coverage & Eval Foundation

### V6-A: Recovery Mode Signal Path (2026-03-25)

Add a second signal path to `screen_day()` that fires during corrections within ongoing
uptrends. The current strategy requires full SMA bull-stack alignment (`price > SMA50 >
SMA100`, `SMA20 > SMA50`), which produces zero candidates in broadly corrective markets
(observed: 80% of tickers rejected at `sma_misaligned` on 2026-03-25).

**Recovery path fires when:**
- `SMA50 > SMA200` — long-term trend structurally intact (no death cross)
- `price ≤ SMA50` — stock is in a correction
- `price > SMA20` — short-term momentum recovering
- All stop, resistance, volume, earnings, and stall guards still apply
- RSI range tightened to 40–65 (vs 50–75 for bull) to target early-recovery entries
- Rule 1b SMA20 slope tolerance relaxed to 1% in recovery mode (vs 0.5% in bull) — a
  stock early in its reversal naturally has a still-declining SMA20

**Changes:**
- `train.py` `screen_day()` — two-path SMA check; `signal_path: "bull" | "recovery"` added
  to return dict; path-appropriate RSI range; relaxed Rule 1b slope floor for recovery
- `screener.py` — `PATH` column in armed/gap-skipped output; `"death_cross"` bucket added
  to `_rejection_reason()` and `_RULES`
- `screener_prepare.py` — `HISTORY_DAYS` 180 → 300 (SMA200 requires ~200 trading days ≈
  290 calendar days; 300 gives a 10-day buffer)
- `tests/test_screener.py` — 7 new unit tests for recovery path
- `tests/test_screener_script.py` — 1 integration test: screener finds ≥ 1 armed candidate
  from synthetic recovery-mode parquet (regression guard for corrective-market coverage)

**Acceptance criteria:**
- [ ] `screen_day()` returns signals when `price < SMA50` provided `SMA50 > SMA200` and `price > SMA20`
- [ ] `screen_day()` bull path unaffected (no regression)
- [ ] `signal_path: "bull" | "recovery"` present in return dict
- [ ] Recovery path silently skipped when hist < 200 rows
- [ ] RSI range 40–65 for recovery, 50–75 for bull
- [ ] Rule 1b slope floor 0.990 for recovery, 0.995 for bull
- [ ] Recovery path blocked when `SMA50 ≤ SMA200` (death cross)
- [ ] `screener.py` PATH column visible in output
- [ ] `_rejection_reason()` returns `"death_cross"` for death-cross rejections
- [ ] All 8 new tests pass: `python -m pytest tests/test_screener.py tests/test_screener_script.py -v`
- [ ] Full suite no new failures: `python -m pytest tests/ -q`

**Cache note (manual, post-execution):**
After bumping `HISTORY_DAYS` to 300, the existing cache must be cleared and rebuilt before
recovery mode fires in the live screener. Rebuild takes ~10 min:
```bash
python -c "import os,glob; from pathlib import Path; [os.remove(f) for f in glob.glob(os.path.join(Path.home(),'.cache','autoresearch','screener_data','*.parquet'))]; print('Cache cleared')"
uv run screener_prepare.py
```

**Plan file:** `.agents/plans/recovery-mode-signal.md`

---

### V6-B: Eval Foundation + Entry Quality (2026-03-25)

Re-baseline the evaluation infrastructure and attack the root cause identified in the
price-volume-updates post-mortem: 89% of trades lived in the 1–5 day bucket at a
near-coin-flip win rate (49%), collectively losing −$120.97. Hypothesis: the 381-ticker
universe generates too many marginal breakouts that immediately reverse. Fix is entry
quality, not exit timing: constrain the universe to high-liquidity tickers via a dollar
volume threshold, and widen evaluation folds so autoresearch improvements signal true
edge rather than sparse-fold noise.

**Changes:**

- `train.py` — `FOLD_TEST_DAYS` 40 → 60, `WALK_FORWARD_WINDOWS` 7 → 6 (6×60 folds
  give ~27 test trades per fold at 150 tickers vs ~17 previously); `MIN_DOLLAR_VOLUME =
  150_000_000` constant added; dollar volume filter in `screen_day()` using
  60-day avg daily dollar volume on yesterday-only data (no look-ahead)
- `program.md` — position management priority moved to iterations 2–4 (highest-leverage
  parameters calibrated before screener work); 110-trade `discard-thin` floor added to
  Goal section; `mean_test_pnl` added as column 3 in results.tsv with `discard-manual-review`
  flag rule; structural vs threshold screener guidance added; Session Override block added
  (Active: NO); Run A Agenda appended (10-iteration plan: position mgmt → dollar volume
  calibration → structural screener additions)

**Acceptance criteria:**
- [ ] `FOLD_TEST_DAYS = 60` and `WALK_FORWARD_WINDOWS = 6` in SESSION SETUP block
- [ ] `MIN_DOLLAR_VOLUME` constant in STRATEGY TUNING block; filter in `screen_day()` using `hist` (no look-ahead)
- [ ] `program.md`: position management priority at iterations 2–4
- [ ] `program.md`: 110-trade `discard-thin` floor in Goal section
- [ ] `program.md`: `mean_test_pnl` logging as column 3 in results.tsv
- [ ] `program.md`: structural vs threshold screener guidance present
- [ ] `program.md`: Session Override block (Active: NO) and Run A Agenda appended
- [ ] Baseline exits 0; `fold6_train_total_trades ≥ 110`; `min_test_pnl_folds_included ≥ 4`
- [ ] Full test suite passes without regressions

**Baseline result:** fold6_train_total_trades = 324, min_test_pnl_folds_included = 6,
min_test_pnl = −3.72 (fold5). MIN_DOLLAR_VOLUME = $150M is well-calibrated at this
universe size; no adjustment needed.

**Plan file:** `.agents/plans/run-a-eval-foundation.md`

---

## Appendix

### Related Documents

- `initial_request.md` — original project analysis and clarifying Q&A
- `example-screener.py` — v2 screener reference implementation (source of baseline strategy)
- `program.md` — (to be rewritten) agent instructions
- `results-v0.tsv` — legacy experiment results from nanochat version (for reference only)

### Key Dependencies

- [yfinance documentation](https://github.com/ranaroussi/yfinance)
- [pandas documentation](https://pandas.pydata.org/docs/)

### Repository Structure (target state)

```
autoresearch/
├── prepare.py              # Fixed: data download + cache
├── train.py                # Mutable: strategy + backtester
├── program.md              # Agent instructions
├── prd.md                  # This document
├── initial_request.md      # Project origin document
├── example-screener.py     # Reference (not used at runtime)
├── pyproject.toml          # Updated dependencies
├── uv.lock                 # Locked dependencies
├── .gitignore              # results.tsv excluded
├── results.tsv             # Untracked experiment log
└── README.md               # Updated project overview
```

**Cache (outside repo):**
```
~/.cache/autoresearch/stock_data/
├── AAPL.parquet
├── MSFT.parquet
└── ...
```
