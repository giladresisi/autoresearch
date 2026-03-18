# Initial Request: Stock Strategy Backtester with LLM Optimization

## Summary

Transform the `autoresearch` project from an LLM pretraining optimizer into a stock trading strategy optimizer. The LLM agent will iteratively modify a backtesting script (`train.py`) containing a screener + position manager, and use the resulting Sharpe ratio to guide its experimentation — the same autonomous loop pattern used for the original nanochat project.

---

## What Changes

### `prepare.py` (data download)
- **Current**: Downloads NLP text parquet shards from HuggingFace, trains a BPE tokenizer.
- **New**: Downloads historical OHLCV stock price data using `yfinance`, for a configurable set of tickers and a configurable date range.
- Data granularity: **one price point per stock per trading day**, sampled ~30 minutes after the US market opens (9:30 AM ET → ~10:00 AM ET bar).
- Stored in a local cache directory (e.g. `~/.cache/autoresearch/stock_data/`) as a CSV or Parquet file per ticker.
- This file is **fixed** and not modified by the LLM agent.

### `train.py` (strategy + backtester)
- **Current**: GPT model definition, optimizer, training loop. Outputs `val_bpb` metric. Requires GPU.
- **New**: Pure Python/pandas/numpy backtester. **No GPU required.** The LLM modifies this file freely to experiment with strategy logic.
- Contains three logical components in one file:
  1. **Screener**: Applies a set of criteria to each day's data to identify candidate entry signals.
  2. **Position Manager**: Maintains open positions and updates stop prices for held stocks.
  3. **Backtester loop**: Iterates through all trading days in the dataset, running the screener and position manager, simulating buys/exits, tracking P&L.
- Outputs a single metric: **Sharpe ratio** over the full backtest period.

### `program.md` (agent instructions)
- **Current**: Instructions for running the nanochat training loop.
- **New**: Instructions for the autonomous strategy optimization loop — same structure, adapted for the stock backtester.

---

## Backtester Logic (as described)

### Data
- One OHLCV row per stock per trading day, representing the state ~30 min after market open.
- The screener and indicators run on this data (e.g. rolling SMAs, CCI, ATR, volume MAs).

### Daily Loop (in chronological order)

**Before running the screener each day:**
1. Check all stocks currently in portfolio: if any stock's **daily low** fell below its stop price on the previous trading day, close that position.
   - Exit price = the stop price (stop was triggered).
   - Calculate realized P&L for the position, add to accumulated P&L.
   - Remove from portfolio.

**Run the screener on today's data:**
2. Apply screener criteria to all tickers using their historical data up to (and including) today.
3. For each match: "buy" the stock.
   - Entry price = today's price + $0.03 slippage.
   - Budget per match: **$500**.
   - Number of shares = floor($500 / entry_price). If entry_price > $500, skip.
   - Record: ticker, entry price, shares, stop price, entry date.
   - Add to portfolio.

**Run the position manager on all currently held positions:**
4. For each held position, re-evaluate whether the stop price should be moved up (trailing stop logic, based on strategy).

### End of Backtest
5. At the end of the date range:
   - Close all remaining open positions at the last available price (mark-to-market).
   - Calculate **Sharpe ratio** over the time series of daily P&L (or per-trade returns — TBD).
6. Print the Sharpe ratio as the final output metric.

### Initial Strategy (baseline)
- The baseline `train.py` should start with the screener logic from `example-screener.py` (v2: SMA150, 3 up days, volume filter, CCI < -50 rising, pullback ≥8% from local high/ATH, candle wick filter, pivot-low stop, resistance filter).
- The initial position manager: raise stop to breakeven when price is ≥ 1 ATR above entry.

---

## Optimization Loop (same pattern as original)

- The LLM autonomously modifies `train.py` (screener criteria, thresholds, position management logic).
- Each run completes the full backtest and prints the Sharpe ratio.
- Results are logged to `results.tsv` (commit hash, Sharpe ratio, status, description).
- If Sharpe improves → keep. If not → revert.
- Loop runs indefinitely until the human stops it.

---

## Key Design Decisions (to confirm)

| # | Topic | Current understanding | Status |
|---|-------|-----------------------|--------|
| 1 | **Ticker universe** | Not yet specified — user will define | ❓ |
| 2 | **Date range** | Not yet specified — user will define | ❓ |
| 3 | **Price point** | ~10:00 AM ET bar (30 min after open) | ❓ |
| 4 | **Stop hit detection** | Use daily Low of the following day | ❓ |
| 5 | **Position sizing** | $500/match, floor(500/price) shares, skip if price > $500 | ❓ |
| 6 | **Slippage** | +$0.03 on entry | ✅ stated |
| 7 | **Sharpe ratio** | Annualized? Risk-free rate? Daily vs. per-trade returns? | ❓ |
| 8 | **Duplicate positions** | Can we enter same ticker twice if it re-qualifies? | ❓ |
| 9 | **Max concurrent positions** | No cap stated — unlimited | ❓ |
| 10 | **Data source for indicators** | Full OHLCV history (open, high, low, close, volume per day) | ✅ implied |
| 11 | **Initial strategy** | example-screener.py v2 logic + breakeven stop manager | ❓ |
| 12 | **Exit at end of backtest** | Mark-to-market at last available price | ❓ |
| 13 | **Dependencies** | yfinance, pandas, numpy — needs to be added to pyproject.toml | ✅ implied |

---

## Technical Notes

- The existing `prepare.py` imports are all NLP/GPU-specific (torch, pyarrow, rustbpe, tiktoken). The new version will be much simpler — primarily yfinance + pandas.
- `train.py` currently requires CUDA. The new version has zero GPU dependency.
- `pyproject.toml` will need updating: remove GPU/NLP deps, add yfinance.
- The existing `results-v0.tsv` and `analysis.ipynb` are legacy artifacts — they won't be removed but will be superseded.
- The `program.md` pattern (branch per run, results.tsv, loop forever) is preserved exactly.
