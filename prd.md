# Product Requirements Document: Stock Strategy Backtester with LLM Optimization

## Executive Summary

**autoresearch** is a CLI-based autonomous research system that uses an LLM agent to iteratively optimize a stock trading strategy through backtesting. The system replaces the original nanochat pretraining loop with a fully CPU-based stock screener, position manager, and backtester — no GPU required.

The LLM agent modifies a single Python script (`train.py`) that embodies a complete trading strategy, runs the backtester against a fixed historical dataset, and uses the resulting Sharpe ratio to decide whether to keep or revert the change. This loop runs indefinitely until manually stopped.

**MVP Goal:** Enable autonomous LLM-driven experimentation on a configurable stock screening and position management strategy, with Sharpe ratio as the optimization target.

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

## Implementation Phases

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
- Walk-forward validation (test on out-of-sample period after optimizing on in-sample)
- Risk-per-trade sizing (size based on ATR rather than fixed $500)

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

## Enhancements (2026-03-20)

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
