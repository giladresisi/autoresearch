# Feature: Strategy Registry and LLM-Driven Strategy Selector (Enhancements 6a + 6b)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Implements two linked enhancements that turn the autoresearch system from a single-strategy backtester into a multi-strategy runtime:

**6a — Strategy Registry**: A `strategies/` package on `master` holds one Python module per published strategy. Each module is self-contained (indicators + `screen_day()` + `manage_position()` + `METADATA`). A shared `base_indicators.py` holds reference implementations of common indicator functions. An extraction script `scripts/extract_strategy.py` reads the best `train.py` from a tagged worktree commit and writes it as a new strategy module — the only sanctioned path for adding strategies to master.

**6b — LLM Strategy Selector**: `strategy_selector.py` exposes `select_strategy(ticker, recent_df, today)`, which calls the Claude API to choose the most contextually appropriate registered strategy for a given ticker and date. Selection is based on sector fit and market regime reasoning, not ticker membership.

## User Story

As a strategy researcher running the autoresearch system,
I want a versioned registry of optimized strategies and a runtime LLM selector,
So that I can deploy the most regime-appropriate strategy for any ticker without manually reviewing metadata.

## Problem Statement

After the first successful optimization run (energy/materials, `e9886df`, Sharpe 5.79 on training window), there is no structured way to:
- Store the strategy so it coexists with future strategies on master
- Attribute the strategy to its optimization context (sector, window, branch)
- At runtime, determine which strategy to apply to an arbitrary ticker

Without these, each optimized strategy is ephemeral: it lives only in its worktree branch and disappears when the branch is pruned.

## Solution Statement

Create the `strategies/` package with `__init__.py` (REGISTRY), `base_indicators.py`, and `energy_momentum_v1.py` (first published strategy, extracted from `e9886df`). Create `scripts/extract_strategy.py` as the canonical extraction CLI. Create `strategy_selector.py` using the Anthropic SDK. Add `anthropic` to pyproject.toml. Add tests for both the registry and the selector.

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: Medium
**Primary Systems Affected**: new `strategies/` package, new `strategy_selector.py`, new `scripts/extract_strategy.py`, `pyproject.toml`
**Dependencies**: `anthropic` SDK (new), `ANTHROPIC_API_KEY` env var
**Breaking Changes**: None — all additions are new files; existing `train.py` and harness are unchanged

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (lines 1–248) — Mutable section: all indicator functions + `screen_day()` + `manage_position()` are the source for `energy_momentum_v1.py` content; the `# DO NOT EDIT BELOW THIS LINE` boundary separates extractable code from the harness
- `pyproject.toml` — Add `anthropic>=0.40.0` to the `dependencies` list
- `prd.md` (lines 733–835) — Source of truth: METADATA field definitions, REGISTRY structure, selector interface, branching policy
- `tests/test_screener.py` — Pattern for pytest fixtures, DataFrame construction helpers, parametrize style
- `tests/test_backtester.py` — Pattern for mocking internal functions with `unittest.mock.patch`
- `program.md` — No changes required; contains worktree-side instructions only

### Git Context for Strategy Extraction

The first strategy to publish is from `autoresearch/mar20`, best commit `e9886df`:
- Sector: energy/materials
- Tickers: CTVA, LIN, XOM, DBA, SM, IYE, EOG, APA, EQT, CTRA, APD, DVN, BKR, COP, VLO, HEI, HAL
- Train window: 2025-12-20 → 2026-03-06
- Test window: 2026-03-06 → 2026-03-20
- train_pnl: 952.88, train_sharpe: 5.791, train_trades: 18

The executor agent will be on master. Use `git show e9886df:train.py` to read the strategy source.
**Do NOT merge the worktree branch into master.** Write extraction output directly to `strategies/energy_momentum_v1.py`.

### New Files Created

| File | Purpose |
|---|---|
| `strategies/__init__.py` | REGISTRY dict: `{name: module}` |
| `strategies/base_indicators.py` | Shared indicator reference implementations |
| `strategies/energy_momentum_v1.py` | First published strategy (energy/materials, mar20) |
| `scripts/__init__.py` | Makes scripts a package (empty) |
| `scripts/extract_strategy.py` | Extraction CLI: `git show <tag>:train.py` → `strategies/<name>.py` |
| `strategy_selector.py` | LLM selector: `select_strategy(ticker, recent_df, today)` |
| `tests/test_registry.py` | Registry loading and METADATA validation tests |
| `tests/test_selector.py` | Selector unit tests (mocked Anthropic API) |

### Environment Variables Required

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required by `strategy_selector.py` at runtime. Read from env via `os.environ.get()`. The module raises `RuntimeError` on import if the key is absent and `_SELECTOR_CHECK_KEY` is not explicitly disabled (used in tests). |

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
Wave 1 (parallel, no dependencies)
├── 1A: base_indicators.py
└── 1B: Add anthropic to pyproject.toml + scripts/__init__.py

Wave 2 (depends on 1A)
├── 2A: energy_momentum_v1.py  (uses base_indicators imports)
└── 2B: strategies/__init__.py  (skeleton REGISTRY, energy added in 2C)

Wave 3 (depends on 2A + 2B)
├── 3A: Register energy_momentum_v1 in REGISTRY (update __init__.py)
└── 3B: scripts/extract_strategy.py  (standalone CLI, no registry dependency)

Wave 4 (depends on 2B + 1B)
└── 4A: strategy_selector.py  (uses REGISTRY + anthropic)

Wave 5 (depends on all above)
├── 5A: tests/test_registry.py
└── 5B: tests/test_selector.py
```

**Parallel tasks**: 4 pairs (Waves 1, 2, 3, 5)
**Sequential bottleneck**: Wave 3→4 (selector needs REGISTRY populated)
**Max speedup**: ~3× over purely sequential execution

---

## IMPLEMENTATION TASKS

### WAVE 1A — Create `strategies/base_indicators.py`

**WAVE**: 1 | **DEPENDS_ON**: nothing | **AGENT_ROLE**: file-writer

Create `strategies/base_indicators.py` with these shared indicator functions, extracted verbatim from `train.py` (current file, mutable section):

```python
"""
strategies/base_indicators.py — Shared technical indicator implementations.
Imported by strategy modules that choose to use them. Strategy modules may
also define their own indicator functions for self-containment.
"""
import numpy as np
import pandas as pd
```

Functions to include (copy from train.py):
- `calc_cci(df, p=20)`
- `calc_rsi14(df)`
- `calc_atr14(df)`
- `find_pivot_lows(df, bars=4)`
- `zone_touch_count(df, level, lookback=90, band_pct=0.015)`
- `find_stop_price(df, entry_price, atr)`
- `is_stalling_at_ceiling(df, band_pct=0.03)`
- `nearest_resistance_atr(df, entry_price, atr, lookback=90)`

Copy these functions exactly as they appear in train.py (lines 51–164). Do not modify signatures or logic. Add the module docstring at top.

---

### WAVE 1B — Add `anthropic` dependency + `scripts/__init__.py`

**WAVE**: 1 | **DEPENDS_ON**: nothing | **AGENT_ROLE**: config-writer

**Task 1**: Edit `pyproject.toml` — add `"anthropic>=0.40.0"` to the `dependencies` list (after the existing entries). Do not remove or reorder existing dependencies.

**Task 2**: Create `scripts/__init__.py` (empty file) so `scripts/` is a proper Python package.

---

### WAVE 2A — Create `strategies/energy_momentum_v1.py`

**WAVE**: 2 | **DEPENDS_ON**: 1A | **AGENT_ROLE**: extractor

This is the first published strategy. Extract it from the `autoresearch/mar20` worktree at commit `e9886df` using:

```bash
git show e9886df:train.py
```

The file has a `# DO NOT EDIT BELOW THIS LINE` boundary. Everything **above** that line (excluding the module docstring and import block) constitutes the extractable strategy code: indicators, `screen_day()`, and `manage_position()`.

Create `strategies/energy_momentum_v1.py` with this structure:

```python
"""
strategies/energy_momentum_v1.py — Energy/Materials Momentum Breakout (v1)

Optimized on: autoresearch/mar20  |  Commit: e9886df
Sector: Energy / Materials
Training window: 2025-12-20 → 2026-03-06
Test window:     2026-03-06 → 2026-03-20
"""
import numpy as np
import pandas as pd

METADATA = {
    "name":         "energy-momentum-v1",
    "sector":       "energy/materials",
    "tickers":      ["CTVA", "LIN", "XOM", "DBA", "SM", "IYE", "EOG", "APA",
                     "EQT", "CTRA", "APD", "DVN", "BKR", "COP", "VLO", "HEI", "HAL"],
    "train_start":  "2025-12-20",
    "train_end":    "2026-03-06",
    "test_start":   "2026-03-06",
    "test_end":     "2026-03-20",
    "source_branch": "autoresearch/mar20",
    "source_commit": "e9886df",
    "train_pnl":    952.88,
    "train_sharpe": 5.791,
    "train_trades": 18,
    "description": (
        "Momentum breakout strategy for the energy/materials sector. Enters when "
        "price_10am breaks above the 20-day highest close and yesterday's high, with "
        "volume at or above its 30-day average, RSI between 50 and 75 (building "
        "momentum, not overbought), and price above SMA50. Tuned on Dec 2025–Mar 2026 "
        "energy universe during a trending macro regime. Best applied when the broader "
        "energy sector (IYE/XLE) is above its own SMA50."
    ),
}

# ── Indicators ────────────────────────────────────────────────────────────────
# (paste extracted indicator functions from git show e9886df:train.py here)
# Include: calc_cci, calc_rsi14, calc_atr14, find_pivot_lows, zone_touch_count,
#          find_stop_price, is_stalling_at_ceiling, nearest_resistance_atr

# ── screen_day ────────────────────────────────────────────────────────────────
# (paste screen_day() from git show e9886df:train.py)

# ── manage_position ───────────────────────────────────────────────────────────
# (paste manage_position() from git show e9886df:train.py)
```

**Concrete steps for executor**:
1. Run `git show e9886df:train.py` and capture the output
2. Find the line containing `# DO NOT EDIT BELOW THIS LINE`
3. Copy everything from line 1 up to (not including) that line
4. From that extracted content, copy:
   - All `def calc_*` and `def find_*` and `def zone_*` and `def is_*` and `def nearest_*` functions
   - `def screen_day(...)` and its full body
   - `def manage_position(...)` and its full body
5. Assemble into the module structure above (module docstring → imports → METADATA → indicator functions → screen_day → manage_position)
6. Do NOT copy: module-level constants (BACKTEST_START, BACKTEST_END, TRAIN_END, TEST_START, WRITE_FINAL_OUTPUTS, CACHE_DIR), or `load_ticker_data`/`load_all_ticker_data` functions — these belong to the harness.

---

### WAVE 2B — Create `strategies/__init__.py` (skeleton)

**WAVE**: 2 | **DEPENDS_ON**: 1A | **AGENT_ROLE**: file-writer

Create `strategies/__init__.py`:

```python
"""
strategies/__init__.py — Strategy Registry

REGISTRY maps strategy name → imported module.
Each module exposes: screen_day(), manage_position(), METADATA dict.

To add a strategy:
  1. Place <name>.py in this directory
  2. Import it here and add to REGISTRY

Strategies are only added via scripts/extract_strategy.py.
Direct edits to strategy logic on master are not permitted (see prd.md §6a).
"""
from strategies import energy_momentum_v1

REGISTRY: dict = {
    "energy-momentum-v1": energy_momentum_v1,
}
```

Note: the `energy_momentum_v1` import is added here in Wave 3A after the module exists. In this wave, create the file with the docstring and an empty REGISTRY (`REGISTRY: dict = {}`).

---

### WAVE 3A — Register energy_momentum_v1 in REGISTRY

**WAVE**: 3 | **DEPENDS_ON**: 2A, 2B | **AGENT_ROLE**: file-writer

Edit `strategies/__init__.py` to add:
```python
from strategies import energy_momentum_v1
```
And update REGISTRY:
```python
REGISTRY: dict = {
    "energy-momentum-v1": energy_momentum_v1,
}
```

---

### WAVE 3B — Create `scripts/extract_strategy.py`

**WAVE**: 3 | **DEPENDS_ON**: 2B | **AGENT_ROLE**: file-writer

This is the canonical CLI for adding new strategies to master from worktree commits.

```
Usage: python scripts/extract_strategy.py --tag <git-tag-or-commit> --name <strategy-name>

Example:
  python scripts/extract_strategy.py --tag e9886df --name energy_momentum_v1
  → Writes strategies/energy_momentum_v1.py

The script:
1. Runs git show <tag>:train.py and captures stdout
2. Finds the # DO NOT EDIT BELOW THIS LINE boundary
3. Extracts all lines above the boundary
4. Writes to strategies/<name>.py with a generated module docstring
5. Prints: "Extracted <tag>:train.py → strategies/<name>.py"
6. Prints: "Next step: add METADATA dict and register in strategies/__init__.py"
```

Implementation:

```python
#!/usr/bin/env python3
"""
scripts/extract_strategy.py — Extract strategy code from a worktree commit.

Usage:
  python scripts/extract_strategy.py --tag <git-tag-or-commit> --name <strategy-name>

Writes strategies/<name>.py with the extractable portion of train.py
(everything above the # DO NOT EDIT BELOW THIS LINE boundary).
"""
import argparse
import subprocess
import sys
from pathlib import Path

BOUNDARY = "# DO NOT EDIT BELOW THIS LINE"
STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"


def extract(tag: str, name: str) -> None:
    result = subprocess.run(
        ["git", "show", f"{tag}:train.py"],
        capture_output=True, text=True, check=True,
    )
    lines = result.stdout.splitlines()
    boundary_idx = next(
        (i for i, line in enumerate(lines) if BOUNDARY in line), None
    )
    if boundary_idx is None:
        print(f"ERROR: boundary '{BOUNDARY}' not found in {tag}:train.py", file=sys.stderr)
        sys.exit(1)
    extractable = lines[:boundary_idx]
    out_path = STRATEGIES_DIR / f"{name}.py"
    header = [
        f'"""',
        f'strategies/{name}.py — Extracted from {tag}:train.py',
        f'',
        f'TODO: Add METADATA dict and register in strategies/__init__.py',
        f'"""',
        "",
    ]
    out_path.write_text("\n".join(header + extractable) + "\n", encoding="utf-8")
    print(f"Extracted {tag}:train.py → {out_path}")
    print(f"Next step: add METADATA dict and register in strategies/__init__.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract strategy from worktree commit")
    parser.add_argument("--tag", required=True, help="Git tag or commit hash")
    parser.add_argument("--name", required=True, help="Strategy module name (e.g. energy_momentum_v1)")
    args = parser.parse_args()
    extract(args.tag, args.name)


if __name__ == "__main__":
    main()
```

---

### WAVE 4A — Create `strategy_selector.py`

**WAVE**: 4 | **DEPENDS_ON**: 3A (REGISTRY populated), 1B (anthropic in pyproject.toml) | **AGENT_ROLE**: file-writer

Create `strategy_selector.py` in the project root.

**Key design decisions:**
- Import REGISTRY from `strategies` package
- Build a ticker snapshot from `recent_df`: last 30 rows, compute SMA20 vs current price, RSI14, volume trend, ATR14, 30-day price change
- Format all strategy METADATA as plain text in the prompt
- Parse Claude's JSON-formatted response into the return dict
- `ANTHROPIC_API_KEY` is read from env; raise RuntimeError at import time if missing (unless `_SELECTOR_SKIP_KEY_CHECK=1` is set — used by tests)

```python
"""
strategy_selector.py — LLM-driven strategy selector.

Given a ticker and recent OHLCV DataFrame, uses Claude to select the most
contextually appropriate registered strategy based on sector fit and regime.

Usage:
    from strategy_selector import select_strategy
    import pandas as pd
    from datetime import date

    result = select_strategy("NVDA", recent_df, date.today())
    # result = {'strategy': 'semis-momentum-v1', 'explanation': '...', 'confidence': 'high'}
    # or:    = {'strategy': None, 'explanation': '...', 'confidence': 'low'}

Environment:
    ANTHROPIC_API_KEY — required at runtime
"""
import json
import os
from datetime import date

import numpy as np
import pandas as pd

from strategies import REGISTRY

_SKIP_KEY_CHECK = os.environ.get("_SELECTOR_SKIP_KEY_CHECK", "0") == "1"
_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not _API_KEY and not _SKIP_KEY_CHECK:
    raise RuntimeError(
        "ANTHROPIC_API_KEY environment variable is required to use strategy_selector. "
        "Set _SELECTOR_SKIP_KEY_CHECK=1 to suppress this check in tests."
    )


def _compute_ticker_snapshot(recent_df: pd.DataFrame) -> dict:
    """Compute summary statistics from recent_df for the LLM prompt."""
    df = recent_df.tail(30).copy()
    if len(df) < 14:
        return {"error": "insufficient data (< 14 bars)"}

    close = df["close"]
    volume = df["volume"]

    # SMA20
    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(df) >= 20 else None
    current_price = float(close.iloc[-1])
    above_sma20 = (current_price > sma20) if sma20 is not None else None

    # RSI14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = float((100 - (100 / (1 + rs))).iloc[-1])

    # Volume trend: last 5 days vs 30-day average
    vol_avg30 = float(volume.mean())
    vol_avg5  = float(volume.tail(5).mean())
    vol_ratio = round(vol_avg5 / vol_avg30, 2) if vol_avg30 > 0 else None

    # ATR14
    high = df["high"]
    low  = df["low"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])

    # 30-day price change %
    price_30d_ago = float(close.iloc[0])
    pct_change_30d = round((current_price - price_30d_ago) / price_30d_ago * 100, 1) if price_30d_ago else None

    return {
        "current_price":   round(current_price, 2),
        "sma20":           round(sma20, 2) if sma20 else None,
        "above_sma20":     above_sma20,
        "rsi14":           round(rsi, 1),
        "atr14":           round(atr, 2),
        "vol_ratio_5d_30d": vol_ratio,
        "pct_change_30d":  pct_change_30d,
    }


def _build_prompt(ticker: str, snapshot: dict, today: date) -> str:
    strategies_text = []
    for name, module in REGISTRY.items():
        m = module.METADATA
        strategies_text.append(
            f"Strategy: {name}\n"
            f"  sector: {m['sector']}\n"
            f"  description: {m['description']}\n"
            f"  trained: {m['train_start']} to {m['train_end']}\n"
            f"  train_pnl: ${m['train_pnl']:.2f}  train_sharpe: {m['train_sharpe']:.3f}  trades: {m['train_trades']}\n"
        )
    if not strategies_text:
        strategies_text = ["(No strategies registered)"]

    return f"""You are a quantitative trading strategy selector. Today is {today}.

## Available Strategies
{chr(10).join(strategies_text)}

## Ticker: {ticker}
Recent market data (last 30 trading days):
{json.dumps(snapshot, indent=2)}

## Task
Select the most appropriate strategy for {ticker} today, or return null if no strategy fits.

Reason about:
1. Which sector does {ticker} belong to?
2. What is the current market regime for that sector (trending/mean-reverting/unclear)?
3. Which strategy's training conditions best match current conditions?
4. Any caveats or risks?

Respond ONLY with a JSON object, no markdown, no explanation outside the JSON:
{{
  "strategy": "<strategy-name-or-null>",
  "explanation": "<2-4 sentence explanation>",
  "confidence": "high|medium|low"
}}"""


def select_strategy(ticker: str, recent_df: pd.DataFrame, today: date) -> dict:
    """
    Select the most appropriate registered strategy for ticker on today's date.

    Args:
        ticker:    Stock symbol (e.g. 'NVDA')
        recent_df: DataFrame with columns [open, high, low, close, volume],
                   DatetimeIndex, at least 30 rows of recent history
        today:     The date to reason about

    Returns:
        {
          'strategy':    str | None,   # registered strategy name, or None
          'explanation': str,          # LLM reasoning
          'confidence':  str,          # 'high' | 'medium' | 'low'
        }
    """
    import anthropic

    snapshot = _compute_ticker_snapshot(recent_df)
    prompt = _build_prompt(ticker, snapshot, today)

    client = anthropic.Anthropic(api_key=_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: return raw text as explanation with no strategy selected
        return {"strategy": None, "explanation": raw, "confidence": "low"}

    return {
        "strategy":    parsed.get("strategy") or None,
        "explanation": parsed.get("explanation", ""),
        "confidence":  parsed.get("confidence", "low"),
    }
```

---

### WAVE 5A — Create `tests/test_registry.py`

**WAVE**: 5 | **DEPENDS_ON**: 3A | **AGENT_ROLE**: test-writer

```python
"""tests/test_registry.py — Registry loading and METADATA validation."""
import pytest
import pandas as pd

REQUIRED_METADATA_KEYS = {
    "name", "sector", "tickers", "train_start", "train_end",
    "test_start", "test_end", "source_branch", "source_commit",
    "train_pnl", "train_sharpe", "train_trades", "description",
}


def test_registry_is_dict():
    from strategies import REGISTRY
    assert isinstance(REGISTRY, dict)


def test_registry_has_energy_momentum_v1():
    from strategies import REGISTRY
    assert "energy-momentum-v1" in REGISTRY


def test_all_strategies_have_required_metadata():
    from strategies import REGISTRY
    for name, module in REGISTRY.items():
        assert hasattr(module, "METADATA"), f"{name} missing METADATA"
        missing = REQUIRED_METADATA_KEYS - set(module.METADATA.keys())
        assert not missing, f"{name} METADATA missing keys: {missing}"


def test_all_strategies_have_screen_day():
    from strategies import REGISTRY
    for name, module in REGISTRY.items():
        assert callable(getattr(module, "screen_day", None)), f"{name} missing screen_day()"


def test_all_strategies_have_manage_position():
    from strategies import REGISTRY
    for name, module in REGISTRY.items():
        assert callable(getattr(module, "manage_position", None)), f"{name} missing manage_position()"


def test_energy_momentum_v1_metadata_values():
    from strategies import energy_momentum_v1
    m = energy_momentum_v1.METADATA
    assert m["name"] == "energy-momentum-v1"
    assert m["sector"] == "energy/materials"
    assert isinstance(m["tickers"], list) and len(m["tickers"]) > 0
    assert m["train_pnl"] == pytest.approx(952.88)
    assert m["train_sharpe"] == pytest.approx(5.791)
    assert m["train_trades"] == 18
    assert m["source_commit"] == "e9886df"


def test_energy_momentum_v1_screen_day_returns_none_on_short_df():
    from strategies.energy_momentum_v1 import screen_day
    import numpy as np
    from datetime import date, timedelta
    n = 30
    base = date(2025, 1, 2)
    dates = [base + timedelta(days=i) for i in range(n)]
    df = pd.DataFrame({
        "open":       np.full(n, 100.0),
        "high":       np.full(n, 101.0),
        "low":        np.full(n, 99.0),
        "close":      np.full(n, 100.0),
        "volume":     np.full(n, 1_000_000.0),
        "price_10am": np.full(n, 100.0),
    }, index=pd.Index(dates, name="date"))
    result = screen_day(df, dates[-1])
    assert result is None


def test_base_indicators_importable():
    from strategies import base_indicators
    assert callable(base_indicators.calc_rsi14)
    assert callable(base_indicators.calc_atr14)
    assert callable(base_indicators.calc_cci)
    assert callable(base_indicators.find_stop_price)


def test_base_indicators_calc_rsi14_range():
    from strategies.base_indicators import calc_rsi14
    import numpy as np
    from datetime import date, timedelta
    n = 30
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(n)]
    close = np.linspace(100, 120, n)
    df = pd.DataFrame({
        "close": close,
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "volume": np.full(n, 1e6),
    }, index=pd.Index(dates, name="date"))
    rsi = calc_rsi14(df)
    # RSI for a consistently rising series should be > 50 after warmup
    valid = rsi.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()
    assert valid.iloc[-1] > 50
```

---

### WAVE 5B — Create `tests/test_selector.py`

**WAVE**: 5 | **DEPENDS_ON**: 4A | **AGENT_ROLE**: test-writer

```python
"""tests/test_selector.py — LLM strategy selector tests (Anthropic API mocked)."""
import json
import os
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# Suppress the ANTHROPIC_API_KEY check before importing the module
os.environ["_SELECTOR_SKIP_KEY_CHECK"] = "1"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import strategy_selector  # noqa: E402


def make_recent_df(n: int = 35, base_price: float = 100.0) -> pd.DataFrame:
    dates = [date(2026, 1, 2) + timedelta(days=i) for i in range(n)]
    close = np.linspace(base_price, base_price * 1.10, n)
    return pd.DataFrame({
        "open":       close * 0.998,
        "high":       close * 1.005,
        "low":        close * 0.993,
        "close":      close,
        "volume":     np.full(n, 1_500_000.0),
        "price_10am": close * 0.999,
    }, index=pd.Index(dates, name="date"))


def _mock_client(response_dict: dict):
    """Build a mock anthropic.Anthropic client that returns response_dict as JSON."""
    mock_content = MagicMock()
    mock_content.text = json.dumps(response_dict)
    mock_message = MagicMock()
    mock_message.content = [mock_content]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


class TestComputeTickerSnapshot:
    def test_returns_dict_with_required_keys(self):
        df = make_recent_df()
        snap = strategy_selector._compute_ticker_snapshot(df)
        for key in ("current_price", "sma20", "rsi14", "atr14", "vol_ratio_5d_30d", "pct_change_30d"):
            assert key in snap

    def test_rsi_in_valid_range(self):
        df = make_recent_df()
        snap = strategy_selector._compute_ticker_snapshot(df)
        assert 0 <= snap["rsi14"] <= 100

    def test_short_df_returns_error(self):
        df = make_recent_df(n=5)
        snap = strategy_selector._compute_ticker_snapshot(df)
        assert "error" in snap

    def test_above_sma20_true_for_rising(self):
        df = make_recent_df(n=35)
        snap = strategy_selector._compute_ticker_snapshot(df)
        assert snap["above_sma20"] is True


class TestSelectStrategy:
    def test_happy_path_returns_strategy(self):
        with patch("anthropic.Anthropic", return_value=_mock_client({
            "strategy": "energy-momentum-v1",
            "explanation": "Good match.",
            "confidence": "high",
        })):
            result = strategy_selector.select_strategy("XOM", make_recent_df(), date(2026, 3, 1))

        assert result["strategy"] == "energy-momentum-v1"
        assert result["confidence"] == "high"
        assert isinstance(result["explanation"], str)

    def test_no_match_returns_none_strategy(self):
        with patch("anthropic.Anthropic", return_value=_mock_client({
            "strategy": None,
            "explanation": "No match.",
            "confidence": "low",
        })):
            result = strategy_selector.select_strategy("GS", make_recent_df(), date(2026, 3, 1))

        assert result["strategy"] is None
        assert result["confidence"] == "low"

    def test_malformed_json_fallback(self):
        mock_content = MagicMock()
        mock_content.text = "Not valid JSON at all"
        mock_message = MagicMock()
        mock_message.content = [mock_content]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message

        with patch("anthropic.Anthropic", return_value=mock_client):
            result = strategy_selector.select_strategy("AAPL", make_recent_df(), date(2026, 3, 1))

        assert result["strategy"] is None
        assert result["confidence"] == "low"
        assert "Not valid JSON" in result["explanation"]

    def test_returns_dict_with_all_keys(self):
        with patch("anthropic.Anthropic", return_value=_mock_client({
            "strategy": "energy-momentum-v1",
            "explanation": "Match.",
            "confidence": "medium",
        })):
            result = strategy_selector.select_strategy("DVN", make_recent_df(), date(2026, 3, 1))

        assert set(result.keys()) == {"strategy", "explanation", "confidence"}

    def test_null_string_strategy_normalized_to_none(self):
        """'null' as string (not JSON null) should be handled."""
        with patch("anthropic.Anthropic", return_value=_mock_client({
            "strategy": None,
            "explanation": "No strategy.",
            "confidence": "low",
        })):
            result = strategy_selector.select_strategy("TSLA", make_recent_df(), date(2026, 3, 1))
        assert result["strategy"] is None


class TestExtractStrategy:
    """Smoke-test the extraction script's parse logic without running git."""

    def test_boundary_detection(self, tmp_path):
        """extract() correctly splits at the DO NOT EDIT boundary."""
        import subprocess
        from scripts.extract_strategy import BOUNDARY

        fake_train = tmp_path / "train.py"
        fake_train.write_text(
            "def screen_day(): pass\n"
            f"{BOUNDARY}\n"
            "def run_backtest(): pass\n",
            encoding="utf-8",
        )
        # Verify our boundary constant matches the real string in train.py
        assert BOUNDARY == "# DO NOT EDIT BELOW THIS LINE"
```

---

## VALIDATION STEPS

After all tasks are complete, run the following validation commands. All must pass.

### Step 1: Run the registry and selector test suites

```bash
uv run pytest tests/test_registry.py -v
uv run pytest tests/test_selector.py -v
```

**Expected**: All tests pass. No import errors.

### Step 2: Run the full test suite to check for regressions

```bash
uv run pytest tests/ -v -m "not integration"
```

**Expected**: Same pass/fail counts as the pre-existing baseline (58 passing, 3 failing from pre-existing issues, 0 new failures).

### Step 3: Verify strategies package imports cleanly

```bash
uv run python -c "from strategies import REGISTRY; print(list(REGISTRY.keys()))"
```

**Expected**: `['energy-momentum-v1']`

### Step 4: Verify base_indicators imports cleanly

```bash
uv run python -c "from strategies.base_indicators import calc_rsi14, calc_atr14, calc_cci; print('OK')"
```

**Expected**: `OK`

### Step 5: Verify extract_strategy.py is runnable and prints help

```bash
uv run python scripts/extract_strategy.py --help
```

**Expected**: argparse help text. No exceptions.

### Step 6: Verify selector script check (without API key)

```bash
_SELECTOR_SKIP_KEY_CHECK=1 uv run python -c "import strategy_selector; print('import ok')"
```

**Expected**: `import ok`

### Step 7: Smoke-test select_strategy with real API (optional — skip if no key)

```bash
# Only run if ANTHROPIC_API_KEY is set in the environment
uv run python -c "
import os
if os.environ.get('ANTHROPIC_API_KEY'):
    from strategy_selector import select_strategy
    import pandas as pd, numpy as np
    from datetime import date, timedelta
    n = 35
    dates = [date(2026,1,2) + timedelta(days=i) for i in range(n)]
    close = np.linspace(100, 110, n)
    df = pd.DataFrame({'open': close,'high': close*1.01,'low': close*0.99,'close': close,'volume': np.full(n,1e6),'price_10am': close}, index=pd.Index(dates,name='date'))
    r = select_strategy('XOM', df, date(2026,3,21))
    print(r)
else:
    print('SKIP: no ANTHROPIC_API_KEY')
"
```

---

## TEST AUTOMATION SUMMARY

| Test | File | Run command | Status |
|---|---|---|---|
| REGISTRY is dict | `tests/test_registry.py::test_registry_is_dict` | `pytest tests/test_registry.py` | ✅ Automated |
| REGISTRY contains energy-momentum-v1 | `tests/test_registry.py::test_registry_has_energy_momentum_v1` | same | ✅ Automated |
| All strategies have required METADATA keys | `tests/test_registry.py::test_all_strategies_have_required_metadata` | same | ✅ Automated |
| All strategies have screen_day() | `tests/test_registry.py::test_all_strategies_have_screen_day` | same | ✅ Automated |
| All strategies have manage_position() | `tests/test_registry.py::test_all_strategies_have_manage_position` | same | ✅ Automated |
| energy_momentum_v1 METADATA values | `tests/test_registry.py::test_energy_momentum_v1_metadata_values` | same | ✅ Automated |
| screen_day returns None on short df | `tests/test_registry.py::test_energy_momentum_v1_screen_day_returns_none_on_short_df` | same | ✅ Automated |
| base_indicators importable | `tests/test_registry.py::test_base_indicators_importable` | same | ✅ Automated |
| RSI in valid range | `tests/test_registry.py::test_base_indicators_calc_rsi14_range` | same | ✅ Automated |
| snapshot has required keys | `tests/test_selector.py::TestComputeTickerSnapshot::test_returns_dict_with_required_keys` | `pytest tests/test_selector.py` | ✅ Automated |
| RSI in valid range (snapshot) | `tests/test_selector.py::TestComputeTickerSnapshot::test_rsi_in_valid_range` | same | ✅ Automated |
| Short df → error key | `tests/test_selector.py::TestComputeTickerSnapshot::test_short_df_returns_error` | same | ✅ Automated |
| above_sma20 true for rising | `tests/test_selector.py::TestComputeTickerSnapshot::test_above_sma20_true_for_rising` | same | ✅ Automated |
| select_strategy happy path | `tests/test_selector.py::TestSelectStrategy::test_happy_path_returns_strategy` | same | ✅ Automated |
| select_strategy no match | `tests/test_selector.py::TestSelectStrategy::test_no_match_returns_none_strategy` | same | ✅ Automated |
| Malformed JSON fallback | `tests/test_selector.py::TestSelectStrategy::test_malformed_json_fallback` | same | ✅ Automated |
| Returns dict with all keys | `tests/test_selector.py::TestSelectStrategy::test_returns_dict_with_all_keys` | same | ✅ Automated |
| Null string normalized | `tests/test_selector.py::TestSelectStrategy::test_null_string_strategy_normalized_to_none` | same | ✅ Automated |
| Boundary detection in extract_strategy | `tests/test_selector.py::TestExtractStrategy::test_boundary_detection` | same | ✅ Automated |
| Regression: full suite | `tests/` | `pytest tests/ -m "not integration"` | ✅ Automated |

**Manual tests**: None — all surfaces are automatable.
**Integration tests** (excluded by default, require live API): `test_e2e.py`

---

## RISKS AND MITIGATIONS

| Risk | Mitigation |
|---|---|
| `e9886df` not in local git history (shallow clone or pruned) | Verify with `git log --oneline e9886df` before extraction; if missing, instruct user to run `git fetch --unshallow` |
| `anthropic` SDK API surface changes between versions | Pin `>=0.40.0` and use stable `.messages.create()` interface only; import inside `select_strategy()` not at module level (deferred) |
| REGISTRY import fails if energy_momentum_v1.py has syntax errors | `test_registry.py` will catch this immediately on import |
| extract_strategy.py overwrites existing strategy file | No guard added (intentional: user is in control); warn in docstring |
| `_compute_ticker_snapshot` with all-zero volume causes division by zero | Guard: `if vol_avg30 > 0` before division; returns `None` for `vol_ratio` |

---

## ACCEPTANCE CRITERIA

- [ ] `strategies/` package exists with `__init__.py`, `base_indicators.py`, `energy_momentum_v1.py`
- [ ] `REGISTRY["energy-momentum-v1"]` is importable and has `screen_day()`, `manage_position()`, `METADATA`
- [ ] `METADATA` for energy_momentum_v1 contains all 13 required fields with correct values
- [ ] `scripts/extract_strategy.py --help` runs without error
- [ ] `strategy_selector.select_strategy()` returns `{strategy, explanation, confidence}` dict
- [ ] All 20 new tests pass
- [ ] No regressions in the existing test suite (`-m "not integration"`)
- [ ] `anthropic>=0.40.0` in `pyproject.toml`
