# PROGRESS

## Status: Phase 2 Code Review Fixes Complete — Ready for Phase 3

## Feature: Phase 1 — Infrastructure Setup
### Planning Phase
**Status**: ✅ Planned
**Plan File**: .agents/plans/phase-1-infrastructure.md

## Feature: Feature 2 — Screener (screen_day)
### Planning Phase
**Status**: ✅ Planned
**Plan File**: .agents/plans/feature-2-screener.md

### Implementation
**Status**: ✅ Executed — all 19 tests passing, changes unstaged

**Files created/modified:**
- `train.py` — full rewrite: `CACHE_DIR`, `load_ticker_data`, 7 indicator helpers, `screen_day` (11 rules), `manage_position` stub
- `tests/__init__.py` — created (empty)
- `tests/test_screener.py` — created (19 tests)

### Acceptance Criteria Validation
**Overall verdict: ACCEPTED** (21/22 PASS, 1 UNVERIFIABLE)

| # | Criterion | Verdict |
|---|-----------|---------|
| 1 | No `import torch`, `import kernels`, or GPU/NLP reference in `train.py` | ✅ PASS |
| 2 | All 7 indicator helpers present and importable | ✅ PASS |
| 3 | `screen_day` implements all 11 rules in exact order | ✅ PASS |
| 4 | `screen_day` uses lowercase column names only | ✅ PASS |
| 5 | `screen_day` uses `price_10am[-1]` (not `close[-1]`) for SMA, pullback, ATR buffer | ✅ PASS |
| 6 | `CACHE_DIR` defined as `~/.cache/autoresearch/stock_data` | ✅ PASS |
| 7 | `load_ticker_data` reads parquet, returns `None` if missing | ✅ PASS |
| 8 | `manage_position` stub present with correct signature | ✅ PASS |
| 9 | `screen_day` returns `None` (not raises) for < 150 rows | ✅ PASS |
| 10 | `screen_day` returns `None` for NaN indicators or zero volume MA30 | ✅ PASS |
| 11 | `screen_day` returns `None` when ATR is zero | ✅ PASS |
| 12 | `screen_day` raises `KeyError` when `price_10am` column is missing | ✅ PASS |
| 13 | `None` from `nearest_resistance_atr` treated as passing in R5 | ✅ PASS |
| 14 | `screen_day` returns only `None` or `dict` | ✅ PASS |
| 15 | Returned dict always contains `'stop'` as `float` | ✅ PASS |
| 16 | `stop < entry_price` always holds | ✅ PASS |
| 17 | `uv run python train.py AAPL` runs without error | ✅ PASS |
| 18 | `screen_day` runs on 10 trailing days of any Parquet file | ⚠️ UNVERIFIABLE — requires Phase 2 parquet cache |
| 19 | Level 1 import command exits 0 | ✅ PASS |
| 20 | `pytest tests/test_screener.py -v` passes 19/19 | ✅ PASS |
| 21 | No `print` inside helper functions or `screen_day` | ✅ PASS |
| 22 | `calc_cci` uses `raw=True` in `.rolling().apply()` | ✅ PASS |

### Code Review Findings — ✅ All Fixed (2026-03-18)

| Severity | Issue | Fix Applied |
|----------|-------|-------------|
| 🔴 High | Vacuous dict-contract tests (`test_return_dict_has_stop_key`, `test_stop_always_below_entry`) | Added `make_signal_df` fixture satisfying all 11 rules; removed `if result is not None:` guards; assertions now always execute |
| 🟡 Medium | `make_passing_df` docstring misleading about Rule 5 | Updated docstring to state it satisfies Rules 1-4 only |
| 🟡 Medium | `is_stalling_at_ceiling` ZeroDivisionError if `h_min == 0` | Added `if h_min == 0: return False` guard (`train.py:108`) |
| 🟢 Low | `c1`/`c2` not in NaN guard in `screen_day` | Added `pd.isna(c1) or pd.isna(c2)` to the NaN guard |
| 🟢 Low | `test_stalling_false_for_trending` relied on implicit linspace spacing | Replaced with explicit high values `[100, 120, 140]` |
| 🟢 Missing | No Rule 4 (CCI not rising) fail-path test | Added `test_rule4_fail_cci_not_rising` |

**Test suite: 20/20 passing** (was 19 tests, added 1 new test for Rule 4 CCI fail path).

**Criterion 18** (real Parquet smoke test) remains unverified — blocked on Phase 3 (`prepare.py`) by design.

### Reports Generated

**Execution Report:** `.agents/execution-reports/feature-2-screener.md`
**Code Review:** `.agents/code-reviews/feature-2-screener.md`

---

---

## Process Learnings

### Phase vs. Feature numbering confusion (2026-03-18)

**What happened:** The PRD defines both *features* (Feature 1, 2, 3 — logical groupings) and *implementation phases* (Phase 1: Infrastructure, Phase 2: Data Layer, Phase 3: Strategy — execution ordering). When asked to "plan feature 2," the planning agent matched on the feature number and planned the Screener, skipping the Data Layer (Phase 2) which the Screener depends on for real-data integration testing.

**The `create-prd` skill is not the issue** — it correctly requires an `## Implementation Phases` section in every PRD. The PRD for this project has it.

**What to do differently:**

- When kicking off a plan, always reference the **Implementation Phases section** of the PRD, not the feature number. Say "plan Phase 2 from the PRD" or "what is the next pending phase?"
- The planning agent should open the PRD, find `## Implementation Phases`, identify which phases are complete vs. pending, and confirm the mapping before writing the plan.
- If a PRD uses both "Feature N" and "Phase N" labels, treat Phase N as the authoritative execution order.

**Current state:** Phase 1 (Infrastructure) ✅ complete. Phase 2 (Data Layer — `prepare.py`) is the correct next step, not yet started.

---

## What Was Done This Session

### Context
Transformed the `autoresearch` project from an LLM-driven nanochat pretraining optimizer into a stock trading strategy optimizer. The structure of the autonomous experiment loop (modify → run → log → keep/revert) is preserved. The optimization target changes from `val_bpb` (lower is better) to Sharpe ratio (higher is better).

### Artifacts Produced

| File | Description |
|------|-------------|
| `initial_request.md` | Structured analysis of the user's request, with a table of open design questions |
| `example-screener.py` | Reference screener implementation (v2) provided by user — source of baseline strategy |
| `prd.md` | Full Product Requirements Document (created via `ai-dev-env:create-prd` skill) |

### Key Decisions Made

| Topic | Decision |
|-------|----------|
| Ticker universe | Fixed `TICKERS = []` placeholder in `prepare.py` — user fills before running |
| Backtest window | Jan 1, 2026 – Mar 1, 2026 (`BACKTEST_START` / `BACKTEST_END` constants) |
| Data granularity | 1h yfinance bars, resampled to daily OHLCV + `price_10am` (Open of 10:00 AM ET bar) |
| Indicator history | Download 2 years before `BACKTEST_START` for SMA150/ATR14/CCI warmup |
| Entry price | `price_10am + $0.03` slippage |
| Position size | $500 / entry_price (fractional shares allowed) |
| Stop detection | Previous day's `low < stop_price` → position closed at stop price |
| Re-entry | Skip ticker if already held in portfolio |
| Sharpe formula | Daily dollar P&L changes, annualized (`× √252`), risk-free rate = 0% |
| End-of-backtest | All open positions closed at last available `price_10am` |
| Baseline strategy | Full v2 screener from `example-screener.py` + breakeven stop manager |
| GPU required | No — pure Python / pandas / numpy |

---

## What the Next Agent Should Do

### Immediate next step: derive implementation plans and execute them

Read `prd.md` in full, then create and execute detailed implementation plans for each phase:

**Phase 1 — Infrastructure**
- Update `pyproject.toml`: remove `torch`, `kernels`, `rustbpe`, `tiktoken`; add `yfinance`
- Run `uv sync`, verify imports

**Phase 2 — Data layer (`prepare.py`)**
- Full rewrite per PRD Feature 1 spec
- User-configurable constants block at top (`TICKERS`, `BACKTEST_START`, `BACKTEST_END`)
- 1h yfinance download → resample to daily OHLCV + `price_10am`
- Cache as one Parquet file per ticker in `~/.cache/autoresearch/stock_data/`

**Phase 3 — Strategy + backtester (`train.py`)**
- Full rewrite per PRD Features 2–5
- Port indicator functions from `example-screener.py`
- Implement `screen_day()`, `manage_position()`, chronological backtester loop
- Fixed-format output block with `sharpe:` line

**Phase 4 — Agent instructions (`program.md`)**
- Rewrite for stock optimization loop (higher Sharpe = keep)
- Updated `results.tsv` schema: `commit`, `sharpe`, `total_trades`, `status`, `description`

**Phase 5 — End-to-end test**
- Set `TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]`
- Run `prepare.py`, verify 5 Parquet files
- Run `train.py`, verify `sharpe:` appears in output
- Validate at least 1 trade occurred in the backtest window

### Key constraint for next agent
Do not commit `results-v0.tsv`, `.env`, or any `prd.backup-*.md` files. Only commit source files.
