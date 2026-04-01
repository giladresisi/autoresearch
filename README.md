# auto-co-trader

An autonomous trading strategy optimizer and real-time signal generator, forked from [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) and adapted for quantitative trading.

Instead of iterating on an LLM training loop, an AI agent here iterates on a stock screener and position management strategy — modifying `train.py`, running a walk-forward backtest, checking if the result improved, keeping or discarding, and repeating. You come back to a log of experiments and (hopefully) a better strategy. The same strategy then powers a real-time screener that produces entry signals and manages stops on open positions.

---

## Join in

**This repo is open for forks, issues, and pull requests.** Whether you want to contribute a better baseline strategy, improve the harness, add new signal types, or share results — all of it helps. The goal is a shared, community-improved strategy optimizer and co-trader that everyone can benefit from. Open an issue or PR, or fork it and take it in your own direction.

---

## How it works

Three files do all the work:

- **`prepare.py`** — one-time data download and caching for all tickers. Fetches OHLCV history via yfinance. Not modified during optimization.
- **`train.py`** — the single file the agent edits. Contains the full strategy: screener (`screen_day`), position manager (`manage_position`), and the walk-forward evaluation harness. **This is what the agent iterates on.**
- **`program.md`** — instructions for the optimization agent: objective, keep/discard criteria, experiment sequence, closed directions, and harness configuration. **This is what you iterate on as the human.**

The agent runs a walk-forward backtest over a historical window, computes fold-level metrics, decides whether to keep or revert the change, and logs the result to `results.tsv`. Repeat for N iterations.

The same `screen_day` and `manage_position` functions in `train.py` are used directly by the real-time screener to generate daily entry signals and stop updates on live positions.

---

## Current configuration

**Optimization harness:**
- **Backtest window:** September 2024 → March 2026 (∼19 months)
- **Universe:** ∼400 tickers across sectors (large-cap US equities)
- **Evaluation:** 6-fold walk-forward cross-validation, 60 business days per test fold
- **Primary objective:** `mean_test_pnl` — arithmetic mean of out-of-sample P&L across all folds (higher is better)
- **Floor constraint:** `min_test_pnl > −$30` (worst single fold must not exceed this loss)
- **Latest baseline:** mean_test_pnl ≈ $251, min_test_pnl ≈ −$3 (commit `141aa8e`)

**Real-time screener:**
- **History window:** 300 days of daily OHLCV per ticker
- **Universe:** Full Russell 1000 + select mega-caps and major indexes
- **Output:** Daily entry signals (bull continuation and recovery paths) + intraday stop levels for open positions

All of the above — universe, window, fold count, objective — are configuration values that can be updated in `prepare.py`, `train.py`, and `program.md` to suit different goals or time periods.

---

## Quick start

**Requirements:** Python 3.10+, [uv](https://docs.astral.sh/uv/). No GPU required — backtests run on CPU.

```bash
# 1. Install uv (if you don't already have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install dependencies
uv sync

# 3. Download and cache ticker data (one-time, takes a few minutes)
uv run prepare.py

# 4. Run a single backtest manually to verify setup
uv run train.py
```

---

## Running the optimization agent

For isolated runs, use the `prepare-optimization` skill first to create a dedicated git worktree so experiments don't touch the master strategy.

### Equity strategy (large-cap US stocks)

Open Claude Code in this repo and prompt:

```
Have a look at program.md and run 30 iterations of the equity strategy optimizer.
```

The agent reads `program.md`, sets up walk-forward constants, runs the baseline, and iterates — modifying `train.py` above the boundary, running the backtest, keeping or reverting each change. Results logged to `results.tsv`.

### SMT divergence strategy (MNQ/MES futures)

Requires IB-Gateway running on port 4002. Download futures data first:

```bash
uv run prepare_futures.py
```

Then open Claude Code and prompt:

```
Have a look at program_smt.md and run 30 iterations of the SMT divergence strategy optimizer.
```

The agent reads `program_smt.md`, runs the baseline on MNQ1!/MES1! 1m data, and iterates — modifying `train_smt.py` above the boundary. Both strategies are fully isolated; optimizing one never touches the other.

---

## Recommended Claude Code skill

Install the [python-performance-optimization](https://skills.sh/wshobson/agents/python-performance-optimization) skill alongside this repo. It profiles and vectorizes the Python code the agent writes, keeping hot paths fast. The optimization agent is instructed to invoke it after every `train.py` edit — numpy vectorization matters when backtesting hundreds of tickers across dozens of folds per iteration.

---

## Project structure

```
prepare.py           — equity data download and caching (yfinance)
prepare_futures.py   — MNQ/MES 1m futures data download (IB-Gateway)
train.py             — equity strategy: screener, position manager, walk-forward harness
train_smt.py         — SMT divergence strategy: signal logic + intraday backtest harness
program.md           — equity optimizer instructions: objective, experiment sequence
program_smt.md       — SMT optimizer instructions: kill zone, divergence tuning
screen.py            — real-time screener (uses screen_day / manage_position from train.py)
data/sources.py      — data source abstraction (yfinance + IB-Gateway)
pyproject.toml       — dependencies
results.tsv          — experiment log (untracked)
strategies/          — registry of named strategy snapshots for reuse across runs
.agents/plans/       — implementation plans
```

---

## Design notes

- **Single file to modify.** The agent only touches `train.py` above the `# DO NOT EDIT BELOW THIS LINE` boundary. Diffs are small and reviewable.
- **Walk-forward evaluation.** Each iteration runs N folds of out-of-sample backtests. The fold structure prevents in-sample overfitting and gives a realistic picture of how the strategy would have performed across different market regimes.
- **Harness and screener are decoupled.** The evaluation harness in `train.py` and the real-time screener in `screen.py` share the same strategy functions, so an improvement validated by the harness automatically applies to live signalling.
- **Multi-strategy potential.** Currently there is a single global strategy applied to all tickers. Splitting into niche strategies — each optimized for a specific sector, signal type, or market regime — would likely produce better metrics and more accurate signals. The harness structure supports this; it's an open direction.

---

## Limitations

The optimization harness is effective at **tweaking and improving an existing strategy** that already shows a positive edge. Given a proven baseline, it reliably finds parameter improvements, entry filters, and exit refinements that hold out-of-sample.

It is less effective at **building a strategy from scratch**. Without a clear starting signal and a well-specified objective, the agent tends to find local improvements that don't generalize. Attempts to optimize for total P&L from a blank slate without constraining the strategy direction have so far not converged to consistently meaningful results. The harness is best used as an optimizer, not a strategy designer.

---

## License

MIT
