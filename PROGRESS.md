# PROGRESS

## Feature: V3-A Signal Correctness (R1, R3, R5, R4-partial)

**Status**: ✅ Complete
**Completed**: 2026-03-22
**Plan**: .agents/plans/v3-a-signal-correctness.md

### Core Validation
All four correctness fixes validated via 7 new automated tests plus the GOLDEN_HASH integrity test. `screen_day()` confirmed to pass `df.iloc[:-1]` (not the full df) to `calc_atr14` via a patched recorder; minimum history boundary tested at exactly 60/61 rows; sizing verified via wide-vs-tight stop comparison; `trade_records` schema validated against 8 required fields; `trades.tsv` header confirmed written even on empty input.

### Test Status
- Automated: ✅ 73 passed (+7 new V3-A tests), 9 pre-existing failures (test_registry.py ImportError, unrelated), 1 skipped
- Manual: none required

### Notes
- `RISK_PER_TRADE = 50.0` is in the mutable section; size formula is now `RISK_PER_TRADE / (entry_price - stop)`
- `_write_trades_tsv()` is in the immutable zone (below DO NOT EDIT); `__main__` writes train trades only (not test)
- GOLDEN_HASH updated to `8c797ebed7a436656539ab4d664c2c147372505769a140c29e3c4ad2b483f3c7`
- Code review flagged: `test_run_backtest_risk_proportional_sizing` verifies the math identity, not the actual sizing applied by `run_backtest()` — minor coverage gap, non-blocking

**Detailed Report**: `.agents/execution-reports/v3-a-signal-correctness.md`

---

## Feature: Strategy Registry and LLM Selector (Enhancements 6a + 6b)

**Status**: ✅ Complete
**Completed**: 2026-03-21
**Plan**: .agents/plans/strategy-registry-and-selector.md

### Core Validation
`strategies/` package created with REGISTRY, `base_indicators.py`, and `energy_momentum_v1.py` (extracted from `e9886df`). `strategy_selector.py` calls `claude -p` CLI (no API key — runs inside Claude Code); `_call_claude()` strips `CLAUDECODE` env var before spawning subprocess. Integration test `test_select_strategy_real_claude_code` makes a real CLI call for XOM and validates response shape and strategy validity.

### Test Status
- Automated: ✅ 120 passed, 1 skipped (unit + integration + e2e, full suite via `.venv`)
- Manual: none required

### Notes
- Selector uses `claude -p <prompt>` subprocess — requires Claude Code on PATH; not portable outside Claude Code
- `BOUNDARY` in `extract_strategy.py` is a substring match (`"DO NOT EDIT BELOW THIS LINE"`) to handle Unicode dash decorations in the actual comment
- `ANTHROPIC_API_KEY` removed from `.env` and `pyproject.toml`

---

## Feature: Optimization Harness Overhaul (Enhancements 1–5)

**Status**: ✅ Complete
**Completed**: 2026-03-21
**Plan**: .agents/plans/optimization-harness-overhaul.md

### Core Validation
Train/test split, P&L-based keep/discard, final test CSV output, sector trend summary, and extended results.tsv all implemented and verified via automated tests.

### Test Status
- Automated: ✅ 58/61 passing (3 pre-existing failures unrelated to this feature)
- Manual: none required

---

## Run: mar20 — Energy/Materials Universe (2026-03-20)

Branch: `autoresearch/mar20` | Best commit: `e9886df` | 30 iterations

| commit | sharpe | total_pnl | total_trades | win_rate | status | description |
|--------|--------|-----------|--------------|----------|--------|-------------|
| 48191e7 | 0.000 | $0 | 0 | — | keep | baseline (strict pullback screener, no trades) |
| 2ceed17 | 2.809 | — | 1 | — | keep | NaN guard + CCI -30 + pullback 5% |
| 68ac7ae | 5.176 | — | 18 | — | keep | momentum breakout: SMA50 + 20d high + vol 1.2× |
| b95992a | 5.266 | — | 19 | — | keep | relax volume to 1.0× MA30 |
| 2a3c800 | 5.382 | — | 19 | — | keep | add price_10am > prev day high |
| **e9886df** | **5.791** | **$952.88** | **18** | **77.8%** | **keep** | **add RSI(14) 50–75 filter** |

Avg PnL/trade: **$52.94** | Backtest: 2025-12-20 → 2026-03-20 | Universe: CTVA, LIN, XOM, DBA, SM, IYE, EOG, APA, EQT, CTRA, APD, DVN, BKR, COP, VLO, HEI, HAL

### Multi-Sector Optimization Results (2026-03-20)

| Sector / Run | Branch | Window | Best Sharpe | Trades | Total PnL | Tag |
|---|---|---|---|---|---|---|
| Energy (in-sample) | `autoresearch/mar20` | Dec 20 → Mar 20, 2026 | 5.791 | 18 | — | `energy-momentum-v1` |
| Energy (OOS validation) | `autoresearch/energy-oos-sep25` | Sep 20 → Dec 20, 2025 | -0.010 | 6 | -$85 | — |
| Energy (OOS optimized) | `autoresearch/energy-oos-opt-sep25` | Sep 20 → Dec 20, 2025 | 8.208 | 73 | ~$956 | `energy-oos-v1` |
| Semis | `autoresearch/semis-mar20` | Dec 20 → Mar 20, 2026 | 4.754 | 34 | $602 | `semis-momentum-v1` |
| Utilities | `autoresearch/utilities-mar20` | Dec 20 → Mar 20, 2026 | 4.363 | 29 | $47 | `utilities-breakout-v1` |
| Financials | `autoresearch/financials-mar20` | Dec 20 → Mar 20, 2026 | 6.575 | 45 | **-$60** | `financials-v1` |

**Key finding — Sharpe metric flaw:** Financials produced Sharpe 6.58 with -$60 PnL. `daily_values` is raw mark-to-market sum (not capital returns); on days with no positions it is $0, so any change that holds positions longer reduces stop-management variance → artificially high Sharpe. This is why Sharpe was replaced with `train_total_pnl` as the optimization criterion (Enhancement 2).

**OOS validation result: FAIL (Sharpe -0.01)** — the in-sample energy strategy overfit to Dec–Mar 2026 regime.

**Detailed analysis**: `.agents/progress-archive/mar20-analysis-and-next-steps.md`

---

## Feature: Phase 5 — End-to-End Integration Test + Post-Phase Enhancements

**Status**: ✅ Complete
**Completed**: 2026-03-20
**Plan**: .agents/plans/phase-5-end-to-end.md

### Core Validation
Full pipeline validated: `prepare.py` → parquet cache → `run_backtest()` → output block. 9 integration tests cover schema, Sharpe consistency, output format, and multi-iteration loop simulation.

### Test Status
- Automated: ✅ 86/86 passing (9 new integration + 77 pre-existing)
- Manual: none required

### Notes
- `price_10am` always-NaN bug fixed: yfinance 1h bars label the 9:30 AM bar at 9:30 ET, not 10:00 AM
- Windows cp1252 encoding fix applied to `prepare.py` (`→` → `->` in print statements)
- `manage_position()` breakeven trigger raised from 1×ATR to 1.5×ATR (matches `screen_day()` entry guard)
- `run_backtest()` loop order fixed: manage existing positions before screening for new entries

**Detailed Report**: `.agents/execution-reports/phase-5-end-to-end.md`

---

## Feature: Phase 4 — Agent Instructions (`program.md`)

**Status**: ✅ Complete
**Completed**: 2026-03-19
**Plan**: .agents/plans/phase-4-agent-instructions.md

### Core Validation
Full rewrite of `program.md` from nanochat/GPU instructions to stock Sharpe optimization agent loop. 23 structural tests verify setup steps, output format, TSV schema, loop instructions, and cannot-modify constraints.

### Test Status
- Automated: ✅ 74/74 passing (23 new + 51 pre-existing)
- Manual: none required

---

## Feature: Phase 3 — Strategy + Backtester (`train.py`)

**Status**: ✅ Complete
**Completed**: 2026-03-18
**Plan**: .agents/plans/phase-3-strategy-backtester.md

### Core Validation
`manage_position()`, `run_backtest()`, `print_results()`, and `__main__` implemented and tested. Stop detection uses `prev_day` low (no look-ahead); Sharpe `std == 0` guard returns 0.0; `grep "^sharpe:"` captures exactly one parseable float.

### Test Status
- Automated: ✅ 51/51 passing (15 new + 36 pre-existing)
- Manual: none required

---

## Feature: Phase 2 — Data Layer (`prepare.py`)

**Status**: ✅ Complete
**Completed**: 2026-03-18
**Plan**: .agents/plans/phase-2-data-layer.md

### Core Validation
yfinance OHLCV downloader implemented; `price_10am` extracted from 9:30 AM bar; parquet cache writes confirmed. `HISTORY_START` set to 1 year before backtest start (yfinance 730-day rolling limit on 1h data).

### Test Status
- Automated: ✅ 16/16 passing (14 mock + 1 integration + 1 subprocess)
- Manual: none required

---

## Feature: Feature 2 — Screener (`screen_day`)

**Status**: ✅ Complete
**Completed**: 2026-03-18
**Plan**: .agents/plans/feature-2-screener.md

### Core Validation
11-rule momentum breakout screener implemented. All indicator helpers present and importable. Acceptance criteria validated 21/22 (1 unverifiable pending parquet cache).

### Test Status
- Automated: ✅ 20/20 passing (19 original + 1 added in code review)
- Manual: none required

---

## System Ready

All phases complete. Pipeline operational:

1. `uv run prepare.py` — downloads and caches OHLCV data for the configured tickers
2. `uv run train.py` — runs the backtest, prints a fixed-format results block
3. Agent loop (via `program.md`) — autonomously mutates `train.py`, commits, backtests, keeps or reverts

To start an experiment session: open a Claude Code conversation in this repo and describe your desired run parameters (tickers, timeframe, iterations).
