"""
train_smt.py — SMT Divergence strategy on MNQ1! (Micro E-mini NASDAQ 100 futures).

Trades MNQ long or short during the NY open kill zone (9:00–10:30 AM ET) using
ICT Smart Money Concepts: SMT divergence between MES and MNQ at session swing
highs/lows, entry confirmation candle, and TDO (True Day Open) as take-profit.

Rewrite strategy constants and functions (above the boundary) to optimize performance.
Do NOT modify anything below: # DO NOT EDIT BELOW THIS LINE
"""
import datetime
import json
import os
import sys
from pathlib import Path

import pandas as pd

# ══ SESSION SETUP ════════════════════════════════════════════════════════════
# Cache directory for futures parquet files.
FUTURES_CACHE_DIR = os.environ.get(
    "FUTURES_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
)

# Backtest window — loaded from futures_manifest.json at module load time below.
# Default values are overridden when the manifest exists.
BACKTEST_START: str = "2024-09-01"
BACKTEST_END:   str = "2026-03-20"

# Train/test split — last 3 calendar days of the backtest window held out as silent holdout.
# These are updated dynamically from BACKTEST_END when the manifest is loaded below.
TRAIN_END   = "2026-03-28"
TEST_START  = "2026-03-28"

# Silent holdout boundary (walk-forward folds end approximately here).
SILENT_END  = "2026-03-28"

# Walk-forward evaluation parameters.
WALK_FORWARD_WINDOWS = 6
FOLD_TEST_DAYS       = 60    # business days per test fold; auto-reduced for short windows
FOLD_TRAIN_DAYS      = 0     # 0 = expanding window (train from BACKTEST_START)

# ── Override from futures_manifest.json (written by prepare_futures.py) ──────
# When prepare_futures.py downloads recent data, it writes the actual start/end
# dates to the manifest. Loading them here keeps the backtest window in sync.
try:
    _manifest_path = Path(FUTURES_CACHE_DIR) / "futures_manifest.json"
    if _manifest_path.exists():
        with open(_manifest_path, encoding="utf-8") as _f:
            _m = json.load(_f)
        BACKTEST_START = _m.get("backtest_start", BACKTEST_START)
        BACKTEST_END   = _m.get("backtest_end",   BACKTEST_END)
        # Set TRAIN_END 3 calendar days before BACKTEST_END to leave a small holdout
        TRAIN_END  = (datetime.date.fromisoformat(BACKTEST_END) - datetime.timedelta(days=3)).isoformat()
        TEST_START = TRAIN_END
        SILENT_END = TRAIN_END
except Exception:
    pass

# Set True only for the special post-loop final test run.
WRITE_FINAL_OUTPUTS = False

# ══ STRATEGY TUNING ══════════════════════════════════════════════════════════
# Kill zone: NY open session window (America/New_York).
SESSION_START = "09:00"
SESSION_END   = "13:30"

# Minimum wall-clock minutes before a divergence signal can fire after session open.
# Used as a timedelta in screen_session, so it is interval-agnostic.
# Set 0 to disable (bar 0 is naturally suppressed by the empty prior-session slice
# in detect_smt_divergence, so the first real signal opportunity is bar 1 regardless).
MIN_BARS_BEFORE_SIGNAL = 0

# Direction filter: "both" = trade longs and shorts | "long" = longs only | "short" = shorts only
# Frozen at "short": post-fix walk-forward shows long PnL negative in 5/6 folds (structural
# asymmetry — SMT divergence at session highs is a stronger signal than at session lows).
TRADE_DIRECTION = "short"

# TDO validity gate: skip signals where the take-profit target is geometrically inverted.
# For LONG: TDO must be above entry (price bounces up to the open).
# For SHORT: TDO must be below entry (price fades down to the open).
# Set False to disable and restore legacy behavior.
TDO_VALIDITY_CHECK = True

# Minimum stop distance in MNQ points. Signals with |entry - stop| < this value are skipped.
# Prevents degenerate sizing when TDO is very close to entry.
# Set 0.0 to disable.
MIN_STOP_POINTS = 2.5

# Per-direction stop placement ratios (fraction of |entry - TDO| distance).
#
# SHORT_STOP_RATIO: optimizer search space [0.25, 0.30, 0.35, 0.40, 0.45] (step 0.05).
# 0.05 was noise-level tight (~3 pts on a 20–40 pt wick instrument → 95% stop-outs).
# Widening to 0.25+ reduces contracts (via position sizer) but raises win rate enough
# to turn expected value positive.
#
# LONG_STOP_RATIO: frozen at 0.05 — longs disabled (TRADE_DIRECTION = "short"),
# value is irrelevant but kept valid to avoid breaking the position-sizing path.
LONG_STOP_RATIO  = 0.05
SHORT_STOP_RATIO = 0.25

# Print per-direction win rate, avg PnL, and exit breakdown after each fold.
# Set False to suppress — does not affect frozen print_results output.
PRINT_DIRECTION_BREAKDOWN = True

# MNQ futures P&L per point per contract.
MNQ_PNL_PER_POINT = 2.0

# Breakeven stop management.
# After the position moves this many MNQ points in our favor, the stop is moved
# to the entry price (breakeven). Set 0.0 to disable.
# Optimizer search space: [0.0, 5.0, 10.0, 15.0, 20.0, 25.0].
BREAKEVEN_TRIGGER_PTS = 0.0

# After breakeven is set, trail the stop this many points behind the best price seen.
# Set 0.0 to disable trailing (stop stays at entry after breakeven triggers).
# Example: 5.0 means stop = best_price + 5.0 for shorts (best_price - 5.0 for longs).
# Optimizer search space: [0.0, 5.0].
TRAIL_AFTER_BREAKEVEN_PTS = 0.0

# Minimum TDO distance filter: skip signals where |entry - TDO| < this value in MNQ pts.
# Filters out degenerate setups where TDO is very close to entry — these produce 4-contract
# positions with ~6 pt stops that are noise-level tight.
# Set 0.0 to disable.
# Optimizer search space: [0.0, 10.0, 15.0, 20.0, 25.0].
MIN_TDO_DISTANCE_PTS = 0.0

# Signal blackout window: skip divergence signals whose entry bar falls in this time range.
# Both values are "HH:MM" strings in the session's local timezone; "" disables the filter.
# Proposed: block 11:00–12:00 (NY morning dead zone, avg −$18.2 across 48 trades).
# Optimizer search space: ["", "11:00"] for START; ["", "12:00"] for END.
SIGNAL_BLACKOUT_START = ""
SIGNAL_BLACKOUT_END   = ""

# Trail-after-TP: instead of exiting at TDO, convert TP into a trailing stop.
# When price first crosses TDO the position stays open; the stop is then trailed
# this many points behind the best post-TDO price. Set 0.0 to disable (exit at TDO).
# Optimizer search space: [0.0, 5.0, 10.0, 20.0].
TRAIL_AFTER_TP_PTS = 0.0


# ══ STRATEGY FUNCTIONS ═══════════════════════════════════════════════════════

def _load_futures_manifest() -> dict:
    """Load futures_manifest.json written by prepare_futures.py."""
    path = Path(FUTURES_CACHE_DIR) / "futures_manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No futures_manifest.json at {path}. Run prepare_futures.py first."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_futures_data() -> dict[str, pd.DataFrame]:
    """Load MNQ and MES futures parquets.

    Checks in priority order:
      1. data/historical/{ticker}.parquet  — Databento permanent store
      2. FUTURES_CACHE_DIR/{interval}/{ticker}.parquet  — IB ephemeral cache
    Returns {"MNQ": df, "MES": df} with tz-aware ET DatetimeIndex.
    Raises FileNotFoundError if parquets are missing (run prepare_futures.py).
    """
    manifest = _load_futures_manifest()
    interval = manifest.get("fetch_interval", "1m")
    result: dict[str, pd.DataFrame] = {}
    for ticker in ["MNQ", "MES"]:
        historical_path = Path("data/historical") / f"{ticker}.parquet"
        ib_path = Path(FUTURES_CACHE_DIR) / interval / f"{ticker}.parquet"
        if historical_path.exists():
            path = historical_path
        elif ib_path.exists():
            path = ib_path
        else:
            raise FileNotFoundError(
                f"Missing futures parquet for {ticker}. Run prepare_futures.py."
            )
        result[ticker] = pd.read_parquet(path)
    # Align MNQ and MES to their common timestamps so run_backtest can apply
    # a single session_mask across both DataFrames without a length mismatch.
    # Bars missing from either instrument are silently dropped from both —
    # correct for SMT divergence which requires simultaneous bars.
    if "MNQ" in result and "MES" in result:
        common_idx = result["MNQ"].index.intersection(result["MES"].index)
        result["MNQ"] = result["MNQ"].loc[common_idx]
        result["MES"] = result["MES"].loc[common_idx]
    return result



def detect_smt_divergence(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    session_start_idx: int,
    _min_bars: int = 0,
) -> str | None:
    """Check for SMT divergence at bar_idx.

    Returns "short" if MES makes new session high but MNQ does not.
    Returns "long"  if MES makes new session low  but MNQ does not.
    Returns None if no divergence or the bar-count guard fires.

    Args:
        mes_bars: OHLCV DataFrame for MES, index = ET datetime (any bar interval)
        mnq_bars: OHLCV DataFrame for MNQ, same index alignment
        bar_idx: current bar position in the session slice
        session_start_idx: first bar index of current session
        _min_bars: Skip bars where bar_idx - session_start_idx < _min_bars.
            Default 0 disables the guard — callers should apply their own
            time-based threshold (e.g. screen_session uses MIN_BARS_BEFORE_SIGNAL
            as a wall-clock timedelta, which is interval-agnostic).
    """
    if bar_idx - session_start_idx < _min_bars:
        return None

    # Compare current bar's extreme against session high/low (excluding current bar)
    session_slice = slice(session_start_idx, bar_idx)
    mes_session_high = mes_bars["High"].iloc[session_slice].max()
    mes_session_low  = mes_bars["Low"].iloc[session_slice].min()
    mnq_session_high = mnq_bars["High"].iloc[session_slice].max()
    mnq_session_low  = mnq_bars["Low"].iloc[session_slice].min()

    cur_mes = mes_bars.iloc[bar_idx]
    cur_mnq = mnq_bars.iloc[bar_idx]

    # Bearish SMT: MES sweeps session high (liquidity grab) but MNQ fails to confirm
    if cur_mes["High"] > mes_session_high and cur_mnq["High"] <= mnq_session_high:
        return "short"
    # Bullish SMT: MES sweeps session low but MNQ fails to confirm
    if cur_mes["Low"] < mes_session_low and cur_mnq["Low"] >= mnq_session_low:
        return "long"
    return None


def find_entry_bar(
    mnq_bars: pd.DataFrame,
    direction: str,
    divergence_idx: int,
    session_end_idx: int,
) -> int | None:
    """Find the confirmation candle after a divergence signal.

    For "short": first bar after divergence_idx where:
        - close < open  (bearish bar)
        - high > close of most recent prior bullish bar (wick pierces bull body)

    For "long": first bar after divergence_idx where:
        - close > open  (bullish bar)
        - low < close of most recent prior bearish bar (wick pierces bear body)

    Returns bar index or None if no confirmation before session_end_idx.
    """
    if direction == "short":
        # Find the most recent bullish bar at or before divergence_idx
        last_bull_close = None
        for i in range(divergence_idx, -1, -1):
            bar = mnq_bars.iloc[i]
            if bar["Close"] > bar["Open"]:
                last_bull_close = bar["Close"]
                break
        if last_bull_close is None:
            return None
        # Confirmation: bearish bar whose wick pierces the bull body close
        for i in range(divergence_idx + 1, session_end_idx):
            bar = mnq_bars.iloc[i]
            if bar["Close"] < bar["Open"] and bar["High"] > last_bull_close:
                return i
    else:  # "long"
        # Find the most recent bearish bar at or before divergence_idx
        last_bear_close = None
        for i in range(divergence_idx, -1, -1):
            bar = mnq_bars.iloc[i]
            if bar["Close"] < bar["Open"]:
                last_bear_close = bar["Close"]
                break
        if last_bear_close is None:
            return None
        # Confirmation: bullish bar whose wick pierces the bear body close
        for i in range(divergence_idx + 1, session_end_idx):
            bar = mnq_bars.iloc[i]
            if bar["Close"] > bar["Open"] and bar["Low"] < last_bear_close:
                return i
    return None


def compute_tdo(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """Return True Day Open = opening price of the 9:30 AM ET bar for given date.

    Falls back to the first available bar on that date if 9:30 bar is absent
    (e.g., for signals detected before 9:30 AM in the 9:00–9:30 window).
    Returns None if no bars exist for the date.
    """
    target_time = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")
    if target_time in mnq_bars.index:
        return float(mnq_bars.loc[target_time, "Open"])
    # Proxy: use the first available bar on that date
    day_bars = mnq_bars[mnq_bars.index.date == date]
    if day_bars.empty:
        return None
    return float(day_bars.iloc[0]["Open"])


def print_direction_breakdown(stats: dict, prefix: str = "") -> None:
    """Print per-direction trade count, win rate, avg PnL, and exit breakdown.

    Uses the same {prefix}{key}: {value} format as print_results so autoresearch
    agents can parse direction metrics alongside the standard fold output.

    Reads from stats["trade_records"]. Prints nothing if trade_records is absent
    or empty. Controlled by PRINT_DIRECTION_BREAKDOWN constant (caller's responsibility
    to check before calling).

    Args:
        stats:  Dict returned by run_backtest or _compute_metrics.
        prefix: String prepended to every printed key (e.g. "fold1_train_").
    """
    trades = stats.get("trade_records", [])
    if not trades:
        return
    for direction in ("long", "short"):
        subset = [t for t in trades if t["direction"] == direction]
        n = len(subset)
        wins = sum(1 for t in subset if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in subset)
        win_rate  = round(wins / n, 4) if n > 0 else 0.0
        avg_pnl   = round(total_pnl / n, 2) if n > 0 else 0.0
        print(f"{prefix}{direction}_trades: {n}")
        print(f"{prefix}{direction}_win_rate: {win_rate}")
        print(f"{prefix}{direction}_avg_pnl: {avg_pnl}")
        exits: dict[str, int] = {}
        for t in subset:
            exits[t["exit_type"]] = exits.get(t["exit_type"], 0) + 1
        for exit_type, count in exits.items():
            print(f"{prefix}{direction}_exit_{exit_type}: {count}")


def screen_session(
    mnq_bars: pd.DataFrame,   # pre-sliced to session window by caller
    mes_bars: pd.DataFrame,   # pre-sliced to session window by caller
    tdo: float,               # True Day Open, pre-computed by caller
) -> dict | None:
    """Run the full SMT signal pipeline for one session.

    Caller is responsible for pre-slicing mnq_bars/mes_bars to the session
    window (SESSION_START–SESSION_END) and pre-computing TDO. This function
    does not reference SESSION_START, SESSION_END, or compute_tdo internally.

    Signal dict keys:
        direction:       "long" | "short"
        entry_price:     float  (close of confirmation bar)
        entry_time:      pd.Timestamp
        take_profit:     float  (TDO)
        stop_price:      float  (entry ± ratio × |entry - TDO|)
        tdo:             float
        divergence_bar:  int    (index within session slice)
        entry_bar:       int    (index within session slice)

    Guards controlled by constants:
        TRADE_DIRECTION:      filter signals by direction ("long", "short", "both")
        TDO_VALIDITY_CHECK:   skip geometrically inverted TDO setups
        MIN_STOP_POINTS:      skip signals with sub-noise stop distances
        LONG_STOP_RATIO:      fraction of |entry - TDO| used for long stop placement
        SHORT_STOP_RATIO:     fraction of |entry - TDO| used for short stop placement
    """
    if mnq_bars.empty or mes_bars.empty:
        return None

    # Reject degenerate TDO before scanning — no valid signal can be placed
    if tdo is None or tdo == 0.0:
        return None

    n_bars = len(mnq_bars)

    # Compute the earliest bar timestamp eligible for a divergence signal.
    # Using a wall-clock timedelta makes this interval-agnostic: MIN_BARS_BEFORE_SIGNAL
    # is treated as minutes, so 10 minutes means "skip bars starting before
    # session_open + 10 min" regardless of whether bars are 1s, 1m, 5m, etc.
    min_signal_ts = mnq_bars.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)

    mes_reset = mes_bars.reset_index(drop=True)
    mnq_reset = mnq_bars.reset_index(drop=True)

    for bar_idx in range(n_bars):
        if mnq_bars.index[bar_idx] < min_signal_ts:
            continue

        direction = detect_smt_divergence(
            mes_reset,
            mnq_reset,
            bar_idx,
            0,
        )
        if direction is None:
            continue

        # Direction filter: skip if this signal's direction is not allowed
        if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
            continue

        # Look for confirmation entry after divergence bar
        entry_idx = find_entry_bar(mnq_reset, direction, bar_idx, n_bars)
        if entry_idx is None:
            continue

        entry_bar = mnq_reset.iloc[entry_idx]
        entry_price = float(entry_bar["Close"])

        # TDO validity gate: skip if TDO is on the wrong side of entry
        if TDO_VALIDITY_CHECK:
            if direction == "long" and tdo <= entry_price:
                continue
            if direction == "short" and tdo >= entry_price:
                continue

        # Per-direction stop placement using configurable ratios
        distance_to_tdo = abs(entry_price - tdo)
        if direction == "long":
            stop_price = entry_price - LONG_STOP_RATIO * distance_to_tdo
        else:
            stop_price = entry_price + SHORT_STOP_RATIO * distance_to_tdo

        # Minimum stop distance guard: reject sub-noise stops
        if MIN_STOP_POINTS > 0 and abs(entry_price - stop_price) < MIN_STOP_POINTS:
            continue

        # Minimum TDO distance guard: skip degenerate setups with very tight TP targets
        if MIN_TDO_DISTANCE_PTS > 0 and distance_to_tdo < MIN_TDO_DISTANCE_PTS:
            continue

        entry_time = mnq_bars.index[entry_idx]

        # Signal blackout filter: skip entries whose bar falls in the configured window
        if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
            t = entry_time.strftime("%H:%M")
            if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                continue

        return {
            "direction":      direction,
            "entry_price":    entry_price,
            "entry_time":     entry_time,
            "take_profit":    tdo,
            "stop_price":     round(stop_price, 4),
            "tdo":            tdo,
            "divergence_bar": bar_idx,
            "entry_bar":      entry_idx,
        }

    return None


def manage_position(
    position: dict,
    current_bar: pd.Series,
) -> str:
    """Check exit conditions for an open position against one bar.

    Returns one of: "hold" | "exit_tp" | "exit_stop" | "exit_time"

    For longs:  stop hit if low  <= stop_price; TP hit if high >= take_profit
    For shorts: stop hit if high >= stop_price; TP hit if low  <= take_profit
    Exit-time is handled by the harness (not this function).

    Breakeven/trailing stop:
        If BREAKEVEN_TRIGGER_PTS > 0 and price has moved that many points in our
        favor, stop_price is moved to entry_price (breakeven). If
        TRAIL_AFTER_BREAKEVEN_PTS > 0, stop trails behind best_price instead.
        Mutations are applied directly to the position dict so subsequent bars
        use the updated stop level. Stop only ever tightens, never widens.

    Trail-after-TP:
        If TRAIL_AFTER_TP_PTS > 0, exit_tp is suppressed when TDO is first
        crossed; instead the stop trails TRAIL_AFTER_TP_PTS points behind the
        best post-TDO price, letting profits run further.
    """
    direction   = position["direction"]
    entry_price = position["entry_price"]
    tp          = position["take_profit"]

    # ── Trail-after-TP: stay in trade past TDO, trail stop behind best price ──
    if TRAIL_AFTER_TP_PTS > 0:
        if position.get("tp_breached"):
            # Already past TDO — update trailing stop each bar
            if direction == "short":
                best = min(position.get("best_after_tp", tp), current_bar["Low"])
                position["best_after_tp"] = best
                position["stop_price"]    = best + TRAIL_AFTER_TP_PTS
            else:
                best = max(position.get("best_after_tp", tp), current_bar["High"])
                position["best_after_tp"] = best
                position["stop_price"]    = best - TRAIL_AFTER_TP_PTS
        else:
            # Check if TDO was crossed this bar for the first time
            crossed = (direction == "short" and current_bar["Low"]  <= tp) or \
                      (direction == "long"  and current_bar["High"] >= tp)
            if crossed:
                position["tp_breached"] = True
                if direction == "short":
                    position["best_after_tp"] = min(tp, current_bar["Low"])
                    position["stop_price"]    = position["best_after_tp"] + TRAIL_AFTER_TP_PTS
                else:
                    position["best_after_tp"] = max(tp, current_bar["High"])
                    position["stop_price"]    = position["best_after_tp"] - TRAIL_AFTER_TP_PTS
                return "hold"

    # ── Breakeven / trailing stop update ─────────────────────────────────────
    # Skip breakeven management once we are trailing past TDO (stop is already ahead of entry)
    if BREAKEVEN_TRIGGER_PTS > 0 and not position.get("tp_breached"):
        # Track the most favorable price excursion seen so far.
        # best_price is initialized lazily from entry_price on first call.
        if direction == "long":
            best      = max(position.get("best_price", entry_price), current_bar["High"])
            excursion = best - entry_price
        else:  # short
            best      = min(position.get("best_price", entry_price), current_bar["Low"])
            excursion = entry_price - best

        position["best_price"] = best

        if excursion >= BREAKEVEN_TRIGGER_PTS:
            if TRAIL_AFTER_BREAKEVEN_PTS > 0:
                # Trail: stop follows best price at a fixed distance behind it
                new_stop = best + TRAIL_AFTER_BREAKEVEN_PTS if direction == "short" else best - TRAIL_AFTER_BREAKEVEN_PTS
            else:
                # Flat breakeven: move stop to entry price
                new_stop = entry_price

            # Only tighten the stop, never widen it
            if direction == "long":
                position["stop_price"] = max(position["stop_price"], new_stop)
            else:
                position["stop_price"] = min(position["stop_price"], new_stop)

    stop = position["stop_price"]

    # ── Exit checks ───────────────────────────────────────────────────────────
    # exit_tp is only used when trail-after-TP is disabled; otherwise the stop
    # takes over once TDO is breached (handled in the block above).
    if direction == "long":
        if current_bar["Low"]  <= stop:                            return "exit_stop"
        if TRAIL_AFTER_TP_PTS == 0 and current_bar["High"] >= tp: return "exit_tp"
    else:  # short
        if current_bar["High"] >= stop:                            return "exit_stop"
        if TRAIL_AFTER_TP_PTS == 0 and current_bar["Low"]  <= tp: return "exit_tp"
    return "hold"


# ─────────────────────────────────────────────────────────────────────────────
# DO NOT EDIT BELOW THIS LINE — harness is frozen
# ─────────────────────────────────────────────────────────────────────────────

# Dollar risk per trade — fixed at $50 to reflect a single-trader risk budget.
# Do NOT change during optimization — risk scaling is not a strategy improvement.
RISK_PER_TRADE = 50.0

# Maximum contracts per trade — reflects a realistic single-trader position limit.
# This cap prevents the optimizer from exploiting degenerate sizing (e.g. a
# 0.001-point stop that implies 50 000 contracts). Do NOT change during optimization.
MAX_CONTRACTS = 4


def _scan_bars_for_exit(
    position: dict,
    bars: pd.DataFrame,
    session_end_ts: "pd.Timestamp | None" = None,
) -> "tuple[str | None, pd.Series | None]":
    """Iterate *bars* and return (exit_result, exit_bar) on the first trigger.

    Interval-agnostic: each bar's end time is inferred as the next bar's start
    timestamp (or *session_end_ts* for the last bar).  If a bar's end time
    reaches or passes *session_end_ts* before a stop/TP fires, the function
    returns ("session_close", bar) so that wide bars (e.g. 5-min, 1-hour) that
    straddle the session boundary are handled correctly regardless of interval.

    Pass session_end_ts=None (default) to suppress the session-close check,
    e.g. when scanning the entry-day remainder where overnight holds are allowed.

    Returns (None, None) if no exit condition is met within the supplied bars.
    """
    n = len(bars)
    for i, (_ts, bar) in enumerate(bars.iterrows()):
        result = manage_position(position, bar)
        if result != "hold":
            return result, bar

        if session_end_ts is not None:
            # Bar end = start of the next bar, or session_end_ts if this is
            # the last bar in the slice (conservative: assume it ends on time).
            bar_end = bars.index[i + 1] if i + 1 < n else session_end_ts
            if bar_end >= session_end_ts:
                return "session_close", bar

    return None, None


def _build_trade_record(
    position: dict,
    exit_result: str,
    exit_bar: pd.Series,
    pnl_per_point: float,
) -> "tuple[dict, float]":
    """Build the trade dict and compute PnL from a closed position."""
    direction_sign = 1 if position["direction"] == "long" else -1
    if exit_result == "exit_tp":
        exit_price = position["take_profit"]
    elif exit_result == "exit_stop":
        exit_price = position["stop_price"]
    else:
        exit_price = float(exit_bar["Close"])

    pnl = (
        direction_sign
        * (exit_price - position["entry_price"])
        * position["contracts"]
        * pnl_per_point
    )

    entry_time = position["entry_time"]
    trade = {
        "entry_date":     str(position["entry_date"]),
        "entry_time":     (
            str(entry_time.time())[:5]
            if hasattr(entry_time, "time")
            else str(entry_time)
        ),
        "exit_time":      (
            str(exit_bar.name.time())[:5]
            if hasattr(exit_bar.name, "time")
            else ""
        ),
        "direction":      position["direction"],
        "entry_price":    round(position["entry_price"], 4),
        "exit_price":     round(exit_price, 4),
        "tdo":            round(position["tdo"], 4),
        "stop_price":     round(position["stop_price"], 4),
        "contracts":      position["contracts"],
        "pnl":            round(pnl, 2),
        "exit_type":      exit_result,
        "divergence_bar": position["divergence_bar"],
        "entry_bar":      position["entry_bar"],
    }
    return trade, pnl


def _compute_fold_params(
    backtest_start: str,
    train_end: str,
    n_folds: int,
    fold_test_days: int,
) -> tuple:
    """Auto-detect short timeframes and return effective fold parameters.

    If total business days from backtest_start to train_end < 130:
      - effective_n_folds = 1
      - effective_fold_test_days = max(1, min(10, total_bdays // 2))
    Otherwise returns (n_folds, fold_test_days) unchanged.
    """
    import pandas as _pd_fold
    total_bdays = len(_pd_fold.bdate_range(backtest_start, train_end))
    short_threshold = 130
    if total_bdays < short_threshold:
        effective_test_days = max(1, min(10, total_bdays // 2))
        return 1, effective_test_days
    return n_folds, fold_test_days


def run_backtest(
    mnq_df: pd.DataFrame,
    mes_df: pd.DataFrame,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Walk-forward intraday backtest for the SMT divergence strategy.

    Interval-agnostic: works with any bar width (1s, 1m, 5m, 1d, mixed).
    Each bar's end time is inferred from the next bar's start timestamp so that
    bars straddling a session boundary are handled correctly.

    For each trading day in [start, end]:
      1. Slice SESSION_START–SESSION_END bars (matched by bar start timestamp)
      2. If no open position: call screen_session(); if signal → open position,
         then immediately scan the remaining session bars for stop/TP/session_close
      3. If open position from a prior day: scan all session bars for exit;
         session_close fires when a bar's end time reaches SESSION_END
      4. Record each closed trade

    Returns a stats dict with all performance metrics.
    """
    start_dt = pd.Timestamp(start or BACKTEST_START).date()
    end_dt   = pd.Timestamp(end   or BACKTEST_END).date()

    trading_days = sorted({
        ts.date() for ts in mnq_df.index
        if start_dt <= ts.date() < end_dt
    })

    position: dict | None = None
    trades: list[dict] = []
    equity_curve: list[float] = [0.0]

    for day in trading_days:
        session_mask = (
            (mnq_df.index.date == day)
            & (mnq_df.index.time >= pd.Timestamp(f"2000-01-01 {SESSION_START}").time())
            & (mnq_df.index.time <= pd.Timestamp(f"2000-01-01 {SESSION_END}").time())
        )
        mnq_session = mnq_df[session_mask].copy()
        mes_session = mes_df[session_mask].copy()

        if mnq_session.empty:
            equity_curve.append(equity_curve[-1])
            continue

        # Session end as a tz-aware timestamp — used by _scan_bars_for_exit to
        # detect bars that straddle the session boundary regardless of bar width.
        session_end_ts = pd.Timestamp(
            f"{day} {SESSION_END}", tz=mnq_session.index.tz
        )

        day_pnl = 0.0

        if position is None:
            mnq_day = mnq_df[mnq_df.index.date == day]
            day_tdo = compute_tdo(mnq_day, day)
            if day_tdo is None:
                equity_curve.append(equity_curve[-1])
                continue
            signal = screen_session(mnq_session, mes_session, day_tdo)
            if signal:
                risk_per_contract = (
                    abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                )
                contracts = (
                    min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
                    if risk_per_contract > 0
                    else 1
                )
                position = {
                    "direction":      signal["direction"],
                    "entry_price":    signal["entry_price"],
                    "entry_time":     signal["entry_time"],
                    "entry_date":     day,
                    "take_profit":    signal["take_profit"],
                    "stop_price":     signal["stop_price"],
                    "tdo":            signal["tdo"],
                    "contracts":      contracts,
                    "divergence_bar": signal["divergence_bar"],
                    "entry_bar":      signal["entry_bar"],
                }
                # Scan remaining bars of the entry session for stop/TP/session_close.
                # Passing session_end_ts ensures the position is always closed by
                # session end — no overnight holds.
                remaining = mnq_session.iloc[signal["entry_bar"] + 1:]
                exit_result, exit_bar = _scan_bars_for_exit(
                    position, remaining, session_end_ts
                )
                if exit_result is not None:
                    trade, day_pnl = _build_trade_record(
                        position, exit_result, exit_bar, MNQ_PNL_PER_POINT
                    )
                    trades.append(trade)
                    position = None
        else:
            # Position carried over from a prior day.  Pass session_end_ts so
            # that bars wider than the remaining session window trigger a
            # session_close rather than being scanned past the boundary.
            exit_result, exit_bar = _scan_bars_for_exit(
                position, mnq_session, session_end_ts
            )

            if exit_result is None:
                exit_result = "session_close"
                exit_bar = mnq_session.iloc[-1]

            trade, day_pnl = _build_trade_record(
                position, exit_result, exit_bar, MNQ_PNL_PER_POINT
            )
            trades.append(trade)
            position = None

        equity_curve.append(equity_curve[-1] + day_pnl)

    # Close any position still open at end of backtest period
    if position is not None:
        last_bars = mnq_df[mnq_df.index.date < end_dt]
        if not last_bars.empty:
            last_bar = last_bars.iloc[-1]
            trade, pnl = _build_trade_record(
                position, "end_of_backtest", last_bar, MNQ_PNL_PER_POINT
            )
            trade["exit_time"] = ""   # no meaningful bar time at backtest end
            trades.append(trade)
            equity_curve.append(equity_curve[-1] + pnl)

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

    # Annualized Sharpe from daily equity changes
    daily_changes = [equity_curve[i] - equity_curve[i - 1] for i in range(1, len(equity_curve))]
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
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd

    calmar = total_pnl / max_dd if max_dd > 0 else 0.0

    exit_types: dict[str, int] = {}
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


def print_results(stats: dict, prefix: str = "") -> None:
    """Print all scalar metrics with an optional prefix for agent parsing."""
    for key, value in stats.items():
        if key in ("trade_records", "exit_type_breakdown"):
            continue
        print(f"{prefix}{key}: {value}")
    for exit_type, count in stats.get("exit_type_breakdown", {}).items():
        print(f"{prefix}exit_{exit_type}: {count}")


if __name__ == "__main__":
    dfs = load_futures_data()
    mnq_df = dfs["MNQ"]
    mes_df = dfs["MES"]

    if mnq_df.empty or mes_df.empty:
        print("No futures data in cache. Run prepare_futures.py first.", file=sys.stderr)
        sys.exit(1)

    import pandas as _pd
    from pandas.tseries.offsets import BDay as _BDay

    _train_end_ts = _pd.Timestamp(TRAIN_END)

    _effective_n_folds, _effective_fold_test_days = _compute_fold_params(
        BACKTEST_START, TRAIN_END, WALK_FORWARD_WINDOWS, FOLD_TEST_DAYS
    )

    fold_test_pnls: list = []

    for _i in range(_effective_n_folds):
        _steps_back         = _effective_n_folds - 1 - _i
        _fold_test_end_ts   = _train_end_ts - _BDay(_steps_back * _effective_fold_test_days)
        _fold_test_start_ts = _fold_test_end_ts - _BDay(_effective_fold_test_days)
        _fold_train_end_ts  = _fold_test_start_ts

        _fold_train_end  = str(_fold_train_end_ts.date())
        _fold_test_start = str(_fold_test_start_ts.date())
        _fold_test_end   = str(_fold_test_end_ts.date())
        _fold_n          = _i + 1

        if FOLD_TRAIN_DAYS > 0:
            _fold_train_start_ts = _fold_train_end_ts - _BDay(FOLD_TRAIN_DAYS)
            _fold_train_start = str(
                max(_fold_train_start_ts.date(), datetime.date.fromisoformat(BACKTEST_START))
            )
        else:
            _fold_train_start = BACKTEST_START

        _fold_train_stats = run_backtest(mnq_df, mes_df, start=_fold_train_start, end=_fold_train_end)
        _fold_test_stats  = run_backtest(mnq_df, mes_df, start=_fold_test_start,  end=_fold_test_end)

        print_results(_fold_train_stats, prefix=f"fold{_fold_n}_train_")
        print_results(_fold_test_stats,  prefix=f"fold{_fold_n}_test_")

        fold_test_pnls.append((_fold_test_stats["total_pnl"], _fold_test_stats["total_trades"]))

    # R2: Exclude folds with < 3 test trades — sparse folds are noise-dominated
    _qualified = [(p, t) for p, t in fold_test_pnls if t >= 3]
    if _qualified:
        min_test_pnl  = min(p for p, t in _qualified)
        mean_test_pnl = sum(p for p, t in _qualified) / len(_qualified)
        _n_included   = len(_qualified)
    else:
        # Sentinel prevents division-by-zero; _n_included counts real folds, not the sentinel.
        _source       = fold_test_pnls if fold_test_pnls else [(0.0, 0)]
        min_test_pnl  = min(p for p, t in _source)
        mean_test_pnl = sum(p for p, t in _source) / len(_source)
        _n_included   = len(fold_test_pnls)

    print("---")
    print(f"mean_test_pnl:               {mean_test_pnl:.2f}")
    print(f"min_test_pnl:                {min_test_pnl:.2f}")
    print(f"min_test_pnl_folds_included: {_n_included}")

    # Silent holdout: [TRAIN_END, BACKTEST_END]
    _silent_stats = run_backtest(mnq_df, mes_df, start=TRAIN_END, end=BACKTEST_END)
    print("---")
    if WRITE_FINAL_OUTPUTS:
        print_results(_silent_stats, prefix="holdout_")
    else:
        print(f"holdout_total_pnl:    {_silent_stats['total_pnl']:.2f}")
        print(f"holdout_total_trades: {_silent_stats['total_trades']}")