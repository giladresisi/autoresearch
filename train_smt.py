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
SHORT_STOP_RATIO = 0.35

# Print per-direction win rate, avg PnL, and exit breakdown after each fold.
# Set False to suppress — does not affect frozen print_results output.
PRINT_DIRECTION_BREAKDOWN = True

# MNQ futures P&L per point per contract.
MNQ_PNL_PER_POINT = 2.0

# Re-entry after mid-session stop-out: allow a second entry on the same divergence.
# Measures how far price moved in the target direction from entry before the stop hit.
# For shorts: move = entry_price − exit_close. If move < threshold, the setup is still
# "loaded" and a new confirmation bar qualifies for re-entry.
# Set 0.0 to disable re-entry entirely.
# Optimizer search space: [0.0, 5.0, 10.0, 20.0, 30.0].
REENTRY_MAX_MOVE_PTS = 999.0

# Pre-TDO progress-based stop lock-in (replaces BREAKEVEN_TRIGGER_PTS).
# Fraction of |entry − TDO| price must travel before stop is moved to entry (breakeven).
# Scale-invariant: 0.65 means "65% of the way to TDO regardless of trade size."
# 0.0 = disable (stop frozen pre-TDO, matching current behaviour).
# Optimizer search space: [0.0, 0.50, 0.60, 0.65, 0.70, 0.75].
BREAKEVEN_TRIGGER_PCT = 0.0

# Maximum bars a trade may remain open after entry (0 = disabled).
# Applies per trade, including re-entries. Exits as "exit_time" at bar N+MAX_HOLD_BARS.
MAX_HOLD_BARS = 120

# Minimum TDO distance filter: skip signals where |entry - TDO| < this value in MNQ pts.
# Filters out setups where TDO is very close to entry.
# Walk-forward evidence: close-TDO setups are net profitable; 15 is the empirically best floor.
# Set 0.0 to disable.
# Optimizer search space: [0.0, 10.0, 15.0, 20.0, 25.0].
MIN_TDO_DISTANCE_PTS = 0.0

# Allowed weekdays for trading (Python weekday: Mon=0 … Sun=6).
# Thursday (3) excluded: 25% win rate vs 40.8% for all other days (Finding 2).
# Set frozenset({0,1,2,3,4}) to re-enable all weekdays.
ALLOWED_WEEKDAYS = frozenset({0, 1, 2, 3, 4})

# Signal blackout window: skip divergence signals whose entry bar falls in this time range.
# Both values are "HH:MM" strings in the session's local timezone; "" disables the filter.
# Blocks 11:00–13:30: 11:xx dead zone + 13:xx drag (only negative-PnL slot, Finding 3).
# Optimizer search space: ["", "11:00"] for START; ["", "13:00", "13:30"] for END.
SIGNAL_BLACKOUT_START = "11:00"
SIGNAL_BLACKOUT_END   = "12:00"

# Trail-after-TP: instead of exiting at TDO, convert TP into a trailing stop.
# When price first crosses TDO the position stays open; the stop is then trailed
# this many points behind the best post-TDO price. Set 0.0 to disable (exit at TDO).
# Optimizer search space: [0.0, 5.0, 10.0, 20.0].
TRAIL_AFTER_TP_PTS = 1.0


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
      1. data/historical/{ticker}_{interval}.parquet  — interval-specific Databento file
      2. data/historical/{ticker}.parquet             — default Databento file
      3. FUTURES_CACHE_DIR/{interval}/{ticker}.parquet — IB ephemeral cache
    Returns {"MNQ": df, "MES": df} with tz-aware ET DatetimeIndex.
    Raises FileNotFoundError if parquets are missing (run prepare_futures.py).
    """
    manifest = _load_futures_manifest()
    interval = manifest.get("fetch_interval", "1m")
    result: dict[str, pd.DataFrame] = {}
    for ticker in ["MNQ", "MES"]:
        interval_path   = Path("data/historical") / f"{ticker}_{interval}.parquet"
        historical_path = Path("data/historical") / f"{ticker}.parquet"
        ib_path         = Path(FUTURES_CACHE_DIR) / interval / f"{ticker}.parquet"
        if interval_path.exists():
            path = interval_path
        elif historical_path.exists():
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


def find_anchor_close(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
) -> float | None:
    """Return the close of the most recent opposite-direction bar at or before bar_idx.

    For "short" setups: looks backward for the most recent bullish bar (close > open).
    For "long"  setups: looks backward for the most recent bearish bar (close < open).

    Returns None if no qualifying bar exists before bar_idx.
    The result is stored as `anchor_close` in the pending-signal state — it is the
    reference price that a confirmation bar must pierce.
    """
    for i in range(bar_idx, -1, -1):
        bar = bars.iloc[i]
        if direction == "short" and bar["Close"] > bar["Open"]:
            return float(bar["Close"])
        if direction == "long" and bar["Close"] < bar["Open"]:
            return float(bar["Close"])
    return None


def is_confirmation_bar(
    bar: pd.Series,
    anchor_close: float,
    direction: str,
) -> bool:
    """Return True if `bar` qualifies as a signal confirmation candle.

    For "short": bar is bearish (close < open) AND high > anchor_close.
    For "long":  bar is bullish (close > open) AND low  < anchor_close.

    This is a single-bar check — the caller iterates bars and calls this each time.
    Replaces the forward scan loop in find_entry_bar().
    """
    if direction == "short":
        return bar["Close"] < bar["Open"] and bar["High"] > anchor_close
    else:  # "long"
        return bar["Close"] > bar["Open"] and bar["Low"] < anchor_close


def screen_session(
    mnq_bars: pd.DataFrame,
    mes_bars: pd.DataFrame,
    tdo: float,
) -> dict | None:
    """Session signal scanner — compatibility shim for signal_smt.py live trading.

    Implements the same scan as the old screen_session using the new helpers
    (find_anchor_close, is_confirmation_bar, _build_signal_from_bar) so that the
    live trading module (signal_smt.py) continues to work unchanged.

    For backtesting use run_backtest() which runs the full bar-by-bar state machine.
    """
    if mnq_bars.empty or mes_bars.empty:
        return None
    if tdo is None or tdo == 0.0:
        return None

    n_bars = min(len(mnq_bars), len(mes_bars))
    min_signal_ts = mnq_bars.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)
    mes_reset = mes_bars.reset_index(drop=True)
    mnq_reset = mnq_bars.reset_index(drop=True)

    for bar_idx in range(n_bars):
        if mnq_bars.index[bar_idx] < min_signal_ts:
            continue

        direction = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0)
        if direction is None:
            continue
        if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
            continue

        ac = find_anchor_close(mnq_reset, bar_idx, direction)
        if ac is None:
            continue

        # Scan forward for first confirmation bar after divergence
        for conf_idx in range(bar_idx + 1, n_bars):
            conf_bar = mnq_reset.iloc[conf_idx]
            if not is_confirmation_bar(conf_bar, ac, direction):
                continue
            entry_time = mnq_bars.index[conf_idx]
            if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
                t = entry_time.strftime("%H:%M")
                if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                    break
            signal = _build_signal_from_bar(conf_bar, entry_time, direction, tdo)
            if signal is None:
                break
            signal["divergence_bar"] = bar_idx
            signal["entry_bar"] = conf_idx
            return signal

    return None


def _build_signal_from_bar(
    bar: pd.Series,
    ts: "pd.Timestamp",
    direction: str,
    tdo: float,
) -> dict | None:
    """Build a signal dict from a confirmed entry bar, applying all validity guards.

    Returns None if the signal fails TDO_VALIDITY_CHECK, MIN_STOP_POINTS, or
    MIN_TDO_DISTANCE_PTS guards. This mirrors the guard logic in the old screen_session().
    """
    entry_price = float(bar["Close"])

    if TDO_VALIDITY_CHECK:
        if direction == "long" and tdo <= entry_price:
            return None
        if direction == "short" and tdo >= entry_price:
            return None

    distance_to_tdo = abs(entry_price - tdo)
    if MIN_TDO_DISTANCE_PTS > 0 and distance_to_tdo < MIN_TDO_DISTANCE_PTS:
        return None

    stop_ratio = SHORT_STOP_RATIO if direction == "short" else LONG_STOP_RATIO
    if direction == "short":
        stop_price = entry_price + stop_ratio * distance_to_tdo
    else:
        stop_price = entry_price - stop_ratio * distance_to_tdo

    if MIN_STOP_POINTS > 0 and abs(entry_price - stop_price) < MIN_STOP_POINTS:
        return None

    return {
        "direction":      direction,
        "entry_price":    entry_price,
        "entry_time":     ts,
        "take_profit":    tdo,
        "stop_price":     round(stop_price, 4),
        "tdo":            tdo,
        "divergence_bar": -1,   # not tracked in bar-loop mode
        "entry_bar":      -1,
    }



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
        If BREAKEVEN_TRIGGER_PCT > 0 and the favorable move expressed as a
        fraction of |entry − TDO| reaches the threshold, stop_price is moved
        to entry_price (breakeven). Mutations are applied directly to the
        position dict so subsequent bars use the updated stop level. Stop only
        ever tightens, never widens.

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
    if BREAKEVEN_TRIGGER_PCT > 0 and not position.get("tp_breached"):
        tdo_dist = abs(entry_price - tp)
        if tdo_dist > 0:
            if direction == "short":
                progress = (entry_price - current_bar["Low"]) / tdo_dist
            else:
                progress = (current_bar["High"] - entry_price) / tdo_dist
            if progress >= BREAKEVEN_TRIGGER_PCT:
                # Only tighten the stop, never widen it
                if direction == "short":
                    position["stop_price"] = min(position["stop_price"], entry_price)
                else:
                    position["stop_price"] = max(position["stop_price"], entry_price)
                position["breakeven_active"] = True

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

# Deprecated — superseded by BREAKEVEN_TRIGGER_PCT. Frozen at 0 to preserve
# backward compatibility. Do not use in strategy logic.
BREAKEVEN_TRIGGER_PTS = 0.0
TRAIL_AFTER_BREAKEVEN_PTS = 0.0

# Dollar risk per trade — fixed at $50 to reflect a single-trader risk budget.
# Do NOT change during optimization — risk scaling is not a strategy improvement.
RISK_PER_TRADE = 50.0

# Maximum contracts per trade — reflects a realistic single-trader position limit.
# This cap prevents the optimizer from exploiting degenerate sizing (e.g. a
# 0.001-point stop that implies 50 000 contracts). Do NOT change during optimization.
MAX_CONTRACTS = 4



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
        # Wick gap: distance from the bar extreme that triggered the stop to bar close.
        # Non-zero only for exit_stop; used by diagnose_bar_resolution.py.
        "stop_bar_wick_pts": (
            round(abs(
                (float(exit_bar["High"]) if position["direction"] == "short" else float(exit_bar["Low"]))
                - float(exit_bar["Close"])
            ), 2)
            if exit_result == "exit_stop" else None
        ),
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

    Per-bar state machine with four states: IDLE, WAITING_FOR_ENTRY, IN_TRADE,
    REENTRY_ELIGIBLE. Allows re-entry within the same session after a stop-out
    when REENTRY_MAX_MOVE_PTS > 0 and the favorable move was below the threshold.

    Returns a stats dict with all performance metrics.
    """
    start_dt = pd.Timestamp(start or BACKTEST_START).date()
    end_dt   = pd.Timestamp(end   or BACKTEST_END).date()

    trading_days = sorted({
        ts.date() for ts in mnq_df.index
        if start_dt <= ts.date() < end_dt
    })

    # State machine variables — persist across days for overnight positions.
    state = "IDLE"           # "IDLE" | "WAITING_FOR_ENTRY" | "IN_TRADE" | "REENTRY_ELIGIBLE"
    pending_direction = None
    anchor_close = None
    position: dict | None = None
    entry_bar_count = 0      # bars since entry, used for MAX_HOLD_BARS
    trades: list[dict] = []
    equity_curve: list[float] = [0.0]

    for day in trading_days:
        # Weekday filter: skip disallowed trading days (e.g. Thursday)
        if day.weekday() not in ALLOWED_WEEKDAYS:
            continue

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

        session_end_ts = pd.Timestamp(
            f"{day} {SESSION_END}", tz=mnq_session.index.tz
        )
        day_pnl = 0.0

        # Compute TDO for new signal generation; skip the day only if TDO is
        # missing AND we are not currently managing a carried position.
        mnq_day = mnq_df[mnq_df.index.date == day]
        day_tdo = compute_tdo(mnq_day, day)
        if day_tdo is None and state != "IN_TRADE":
            equity_curve.append(equity_curve[-1])
            continue

        # Reset pending state at day boundary — divergence signals are session-scoped.
        # An open position (IN_TRADE) is allowed to carry across days.
        if state != "IN_TRADE":
            state = "IDLE"
            pending_direction = None
            anchor_close = None

        min_signal_ts = mnq_session.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)

        # Pre-compute reset-index views once per session to avoid repeated resets in the loop.
        mes_reset = mes_session.reset_index(drop=True)
        mnq_reset = mnq_session.reset_index(drop=True)

        for bar_idx, (ts, bar) in enumerate(mnq_session.iterrows()):

            if state == "IN_TRADE":
                entry_bar_count += 1
                result = manage_position(position, bar)

                # Time-based exit: close after MAX_HOLD_BARS bars regardless of TP/stop.
                if MAX_HOLD_BARS > 0 and entry_bar_count >= MAX_HOLD_BARS and result == "hold":
                    result = "exit_time"

                # Session-end forced close: bar end reaches or passes session boundary.
                bar_end = (
                    mnq_session.index[bar_idx + 1]
                    if bar_idx + 1 < len(mnq_session)
                    else session_end_ts
                )
                if bar_end >= session_end_ts and result == "hold":
                    result = "session_close"

                if result != "hold":
                    trade, day_pnl_delta = _build_trade_record(
                        position, result, bar, MNQ_PNL_PER_POINT
                    )
                    trades.append(trade)
                    day_pnl += day_pnl_delta

                    # Determine re-entry eligibility after a stop-out or time exit.
                    if REENTRY_MAX_MOVE_PTS > 0 and result in ("exit_stop", "exit_time"):
                        if position.get("breakeven_active"):
                            # Stop was at breakeven — price never really moved; always eligible.
                            state = "REENTRY_ELIGIBLE"
                            anchor_close = float(bar["Close"])
                        else:
                            if position["direction"] == "short":
                                move = position["entry_price"] - float(bar["Close"])
                            else:
                                move = float(bar["Close"]) - position["entry_price"]
                            if move < REENTRY_MAX_MOVE_PTS:
                                state = "REENTRY_ELIGIBLE"
                                anchor_close = float(bar["Close"])
                            else:
                                state = "IDLE"
                    else:
                        state = "IDLE"

                    pending_direction = position["direction"] if state == "REENTRY_ELIGIBLE" else None
                    position = None
                    entry_bar_count = 0

            elif state == "WAITING_FOR_ENTRY":
                # Apply blackout to entry bar — keeps parity with screen_session() check.
                if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
                    t = ts.strftime("%H:%M")
                    if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                        continue
                if anchor_close is not None and is_confirmation_bar(bar, anchor_close, pending_direction):
                    signal = _build_signal_from_bar(bar, ts, pending_direction, day_tdo)
                    if signal is not None:
                        risk_per_contract = (
                            abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                        )
                        contracts = (
                            min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
                            if risk_per_contract > 0 else 1
                        )
                        position = {**signal, "entry_date": day, "contracts": contracts}
                        state = "IN_TRADE"
                        entry_bar_count = 0

            elif state == "REENTRY_ELIGIBLE":
                # Apply blackout to re-entry bar, same as initial entry.
                if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
                    t = ts.strftime("%H:%M")
                    if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                        continue
                if anchor_close is not None and is_confirmation_bar(bar, anchor_close, pending_direction):
                    signal = _build_signal_from_bar(bar, ts, pending_direction, day_tdo)
                    if signal is not None:
                        risk_per_contract = (
                            abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                        )
                        contracts = (
                            min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
                            if risk_per_contract > 0 else 1
                        )
                        position = {**signal, "entry_date": day, "contracts": contracts}
                        state = "IN_TRADE"
                        entry_bar_count = 0

            else:  # IDLE
                if day_tdo is None:
                    continue
                if ts < min_signal_ts:
                    continue

                # Blackout filter applied at bar level in the state machine.
                if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
                    t = ts.strftime("%H:%M")
                    if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                        continue

                direction = detect_smt_divergence(
                    mes_reset,
                    mnq_reset,
                    bar_idx,
                    0,
                )
                if direction is None:
                    continue
                if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
                    continue

                ac = find_anchor_close(mnq_reset, bar_idx, direction)
                if ac is None:
                    continue
                pending_direction = direction
                anchor_close = ac
                state = "WAITING_FOR_ENTRY"

        # End of session: force-close any position still open at session boundary.
        if state == "IN_TRADE" and position is not None:
            last_bar = mnq_session.iloc[-1]
            trade, day_pnl_delta = _build_trade_record(
                position, "session_close", last_bar, MNQ_PNL_PER_POINT
            )
            trades.append(trade)
            day_pnl += day_pnl_delta
            position = None
            entry_bar_count = 0

        # Reset all pending state at day boundary — signals don't carry across days.
        state = "IDLE"
        pending_direction = None
        anchor_close = None

        equity_curve.append(equity_curve[-1] + day_pnl)

    # Safety net: close any position still open at end of the backtest period.
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


def _write_results_tsv(row: dict) -> None:
    """Append one experiment row to results.tsv (tab-separated). Creates with header if missing.
    Schema matches first-smt-opt/results.tsv. status and description are left blank."""
    import csv
    import subprocess

    fieldnames = [
        "iter", "commit", "mean_test_pnl", "min_test_pnl", "total_test_trades",
        "avg_win_rate", "avg_rr", "avg_sharpe", "avg_calmar",
        "avg_expectancy", "wl_ratio", "status", "description",
    ]
    path = "results.tsv"
    # iter = number of data rows already in the file (header not counted)
    try:
        with open(path, encoding="utf-8") as _f:
            _lines = [l for l in _f if not l.startswith("iter\t") and l.strip()]
        _iter = len(_lines)
    except FileNotFoundError:
        _iter = 0
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    try:
        commit = subprocess.check_output(
            ["git", "log", "--format=%h", "-1"], text=True
        ).strip()
    except Exception:
        commit = "unknown"
    row["iter"] = _iter
    row["commit"] = commit
    row.setdefault("status", "")
    row.setdefault("description", "")
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


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
    _fold_test_stats_list: list = []  # collect per-fold stats for TSV aggregates

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
        _fold_test_stats_list.append(_fold_test_stats)

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

    # Compute per-fold averages for results.tsv (only folds with trades)
    _active = [s for s in _fold_test_stats_list if s["total_trades"] >= 3]
    _n_act  = len(_active) or 1
    _avg_wr  = sum(s["win_rate"]           for s in _active) / _n_act
    _avg_rr  = sum(s["avg_rr"]             for s in _active) / _n_act
    _avg_sh  = sum(s["sharpe"]             for s in _active) / _n_act
    _avg_cal = sum(s["calmar"]             for s in _active) / _n_act
    _avg_exp = sum(s["avg_pnl_per_trade"]  for s in _active) / _n_act
    _wl      = _avg_wr / (1 - _avg_wr) if _avg_wr < 1.0 else float("inf")
    _write_results_tsv({
        "mean_test_pnl":    f"{mean_test_pnl:.2f}",
        "min_test_pnl":     f"{min_test_pnl:.2f}",
        "total_test_trades": sum(t for _, t in fold_test_pnls),
        "avg_win_rate":     f"{_avg_wr:.4f}",
        "avg_rr":           f"{_avg_rr:.4f}",
        "avg_sharpe":       f"{_avg_sh:.4f}",
        "avg_calmar":       f"{_avg_cal:.4f}",
        "avg_expectancy":   f"{_avg_exp:.2f}",
        "wl_ratio":         f"{_wl:.4f}",
    })