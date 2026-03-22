# Product Requirements Document: Stock Strategy Backtester with LLM Optimization

## Executive Summary

**autoresearch** is a CLI-based autonomous research system that uses an LLM agent to iteratively optimize a stock trading strategy through backtesting. The system replaces the original nanochat pretraining loop with a fully CPU-based stock screener, position manager, and backtester — no GPU required.

The LLM agent modifies a single Python script (`train.py`) that embodies a complete trading strategy, runs the backtester against a fixed historical dataset, and uses the resulting Sharpe ratio to decide whether to keep or revert the change. This loop runs indefinitely until manually stopped.

**MVP Goal:** Enable autonomous LLM-driven experimentation on a configurable stock screening and position management strategy, with Sharpe ratio as the optimization target.

The system is developed in three versioned harness generations, each building on the previous:

| Version | Scope | Date |
|---------|-------|------|
| **V1** | Initial implementation: data pipeline, backtester, Sharpe metric, agent loop | 2026-01 |
| **V2** | Train/test split, P&L objective, final outputs, sector registry, strategy selector | 2026-03-20 |
| **V3** | Harness robustness: look-ahead fix, risk-proportional sizing, walk-forward CV, robustness controls | 2026-03-22 |

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

V3 is organized into three sequential implementation plans, each executable as a single plan+execution session. **Run in order** — V3-B's walk-forward results are more meaningful after V3-A's sizing fix, and V3-C layers on top of the new framework.

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

### V3 Compatibility Notes

- V3-A, V3-B, V3-C each touch the immutable zone → each requires a `GOLDEN_HASH` update in `tests/test_optimization.py`
- **Sequence dependency:** V3-A → V3-B → V3-C. V3-A must precede V3-B (sizing fix makes rolling-window results meaningful). V3-C can be parallelised with V3-B if needed but is designed to layer on top.
- All V3 mutable-section constants are agent-tunable during optimization runs
- The `strategies/` registry (V2 Enhancement 6) and `scripts/fetch_all_strategies.py` are unaffected by V3
- **Out-of-scope items from V1 that V3 now addresses:** "Max concurrent positions cap" → R8; "Walk-forward validation" → R2; "Risk-per-trade sizing" → R3

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
