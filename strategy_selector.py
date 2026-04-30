"""
strategy_selector.py — LLM-driven strategy selector.

Given a ticker and recent OHLCV DataFrame, calls the Claude Code CLI to select
the most contextually appropriate registered strategy based on sector fit and
market regime reasoning — not ticker membership in a training universe.

Requires the `claude` CLI to be on PATH (provided by Claude Code).
No API key configuration needed; authentication is handled by Claude Code.

Usage:
    from strategy_selector import select_strategy
    import pandas as pd
    from datetime import date

    result = select_strategy("XOM", recent_df, date.today())
    # {'strategy': 'energy-momentum-v1', 'explanation': '...', 'confidence': 'high'}
    # or:
    # {'strategy': None, 'explanation': '...', 'confidence': 'low'}
"""
import json
import os
import subprocess
from datetime import date

import pandas as pd

from strategies import REGISTRY


def _call_claude(prompt: str) -> str:
    """
    Invoke the claude CLI with the given prompt and return the response text.

    Strips CLAUDECODE and ANTHROPIC_API_KEY from the subprocess environment.
    CLAUDECODE: prevents the CLI from detecting it is already inside a Claude
      Code session and behaving differently (e.g. refusing to spawn).
    ANTHROPIC_API_KEY: if a .env file loaded this key (e.g. via load_dotenv),
      the subprocess would use it for API-key auth instead of the CLI's own
      OAuth session — which can fail if the key lacks CLI-specific permissions.
    """
    _STRIP = {"CLAUDECODE", "ANTHROPIC_API_KEY"}
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def _compute_ticker_snapshot(recent_df: pd.DataFrame) -> dict:
    """
    Compute summary statistics from recent_df for inclusion in the LLM prompt.
    Returns a dict of human-readable metrics; on insufficient data returns {'error': ...}.
    """
    df = recent_df.tail(30).copy()
    if len(df) < 14:
        return {"error": "insufficient data (< 14 bars)"}

    close  = df["close"]
    volume = df["volume"]

    # SMA20: requires at least 20 bars
    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(df) >= 20 else None
    current_price = float(close.iloc[-1])
    above_sma20   = (current_price > sma20) if sma20 is not None else None

    # RSI(14) using standard Wilder smoothing
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, float("nan"))
    rsi   = float((100 - (100 / (1 + rs))).iloc[-1])

    # Volume trend: recent 5-day average vs 30-day average
    vol_avg30 = float(volume.mean())
    vol_avg5  = float(volume.tail(5).mean())
    # Guard against zero volume (e.g. synthetic test data)
    vol_ratio = round(vol_avg5 / vol_avg30, 2) if vol_avg30 > 0 else None

    # ATR(14)
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
    pct_change_30d = (
        round((current_price - price_30d_ago) / price_30d_ago * 100, 1)
        if price_30d_ago != 0 else None
    )

    return {
        "current_price":    round(current_price, 2),
        "sma20":            round(sma20, 2) if sma20 is not None else None,
        "above_sma20":      above_sma20,
        "rsi14":            round(rsi, 1),
        "atr14":            round(atr, 2),
        "vol_ratio_5d_30d": vol_ratio,
        "pct_change_30d":   pct_change_30d,
    }


def _build_prompt(ticker: str, snapshot: dict, today: date) -> str:
    """Format all registered strategy metadata and ticker snapshot into a selector prompt."""
    strategies_text = []
    for name, module in REGISTRY.items():
        m = module.METADATA
        pnl_str = "N/A" if m["train_pnl"] is None else f"${m['train_pnl']:.2f}"
        strategies_text.append(
            f"Strategy: {name}\n"
            f"  sector: {m['sector']}\n"
            f"  description: {m['description']}\n"
            f"  trained: {m['train_start']} to {m['train_end']}\n"
            f"  train_pnl: {pnl_str}  "
            f"train_sharpe: {m['train_sharpe']:.3f}  "
            f"trades: {m['train_trades']}\n"
        )

    strategies_block = (
        "\n".join(strategies_text) if strategies_text else "(No strategies registered)"
    )

    return f"""You are a quantitative trading strategy selector. Today is {today}.

## Available Strategies
{strategies_block}

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

Respond ONLY with a JSON object — no markdown fences, no preamble, just the raw JSON:
{{
  "strategy": "<strategy-name or null>",
  "explanation": "<2-4 sentence explanation>",
  "confidence": "high|medium|low"
}}"""


def select_strategy(ticker: str, recent_df: pd.DataFrame, today: date) -> dict:
    """
    Select the most appropriate registered strategy for ticker on today's date.

    Args:
        ticker:    Stock symbol (e.g. 'XOM')
        recent_df: DataFrame with columns [open, high, low, close, volume],
                   DatetimeIndex, at least 30 rows of recent history
        today:     The date to reason about

    Returns:
        {
          'strategy':    str | None,   # registered strategy name, or None if no match
          'explanation': str,          # LLM reasoning
          'confidence':  str,          # 'high' | 'medium' | 'low'
        }
    """
    snapshot = _compute_ticker_snapshot(recent_df)
    prompt   = _build_prompt(ticker, snapshot, today)
    raw      = _call_claude(prompt)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Claude returned non-JSON (e.g. wrapped in markdown fences) — return raw as explanation
        return {"strategy": None, "explanation": raw, "confidence": "low"}

    return {
        "strategy":    parsed.get("strategy") or None,
        "explanation": parsed.get("explanation", ""),
        "confidence":  parsed.get("confidence", "low"),
    }
