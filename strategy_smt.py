"""strategy_smt.py — SMT Divergence strategy constants and functions. Fully mutable — owned by the optimizing agent."""
import datetime
import json
import os
import sys
from pathlib import Path

import pandas as pd

# Cache directory for futures parquet files.
FUTURES_CACHE_DIR = os.environ.get(
    "FUTURES_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
)

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
# Re-testing "both" with quality filters active (MAX_TDO=15, STOP_RATIO=0.35).
# Previous short-only verdict was pre-filter; longs+Thursdays now evaluated on equal footing.
TRADE_DIRECTION = "both"

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
LONG_STOP_RATIO  = 0.35
SHORT_STOP_RATIO = 0.35

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
SIGNAL_BLACKOUT_END   = "13:00"

# Trail-after-TP: instead of exiting at TDO, convert TP into a trailing stop.
# When price first crosses TDO the position stays open; the stop is then trailed
# this many points behind the best post-TDO price. Set 0.0 to disable (exit at TDO).
# Optimizer search space: [0.0, 5.0, 10.0, 20.0].
TRAIL_AFTER_TP_PTS = 1.0

# Maximum TDO distance filter: skip signals where |entry - TDO| > this value in MNQ pts.
# Cross-tab finding: TDO<20 has WR=37-43% and EP=$32-$59 across ALL re-entry sequences,
# including 5th+. TDO>100 trades are structurally losing (EP=−$2.04). TDO>50 barely break
# even. The quality degradation at high re-entry counts is driven by TDO distance, not depth.
# Optimizer search space: [15, 20, 25, 30, 40, 999].
# Set 999.0 to disable (pass-through for all distances).
MAX_TDO_DISTANCE_PTS = 15.0

# Maximum re-entries per session day.
# At TDO<20 (with MAX_TDO_DISTANCE_PTS applied), even Seq#5+ has EP=$32, so this filter
# is less important than expected. Most useful at TDO 20-50 where Seq#5+ declines to EP=$6.
# Optimizer search space: [1, 2, 3, 4, 999]. Default 999 = disabled.
MAX_REENTRY_COUNT = 1

# Minimum bars the prior trade must have survived before re-entry is allowed.
# DIAGNOSTIC ONLY — do not include in optimization runs. Extended diagnostics showed:
# prior_bars<3 (n=1036, 42% of re-entries) has EP=$16.39 — removing these hurts volume
# without improving EP. WR bumps at 10+ bars but EP stays flat. At TDO<20, prior duration
# is irrelevant. Set 0 to disable (always allow re-entry).
MIN_PRIOR_TRADE_BARS_HELD = 0

# Minimum MES sweep magnitude for SMT divergence: how far MES must exceed the prior
# session extreme to qualify. Marginal sweeps (< 1 pt) are noise.
# Optimizer search space: [0, 1, 2, 5].
# Set 0.0 to disable.
MIN_SMT_SWEEP_PTS = 0.0

# Minimum MNQ miss magnitude for SMT divergence: how far MNQ must fail to match MES.
# A strong divergence (MNQ missed by 3 pts) is more reliable than a marginal one (0.5 pt).
# Optimizer search space: [0, 1, 2, 5].
# Set 0.0 to disable.
MIN_SMT_MISS_PTS = 0.0

# Midnight open as TP target (replaces 9:30 RTH open / TDO).
# ICT canonical intraday reversion target = first 1m bar at/after 00:00 ET.
# Optimizer search space: [True, False]
MIDNIGHT_OPEN_AS_TP: bool = False

# Structural stop placement: stop beyond the divergence bar's wick extreme.
# When False: ratio × |entry - TP| (current behavior).
# STRUCTURAL_STOP_BUFFER_PTS: points beyond the wick to place the stop.
# Optimizer search space: STRUCTURAL_STOP_MODE [True, False];
#   STRUCTURAL_STOP_BUFFER_PTS [1.0, 2.0, 3.0, 5.0]
STRUCTURAL_STOP_MODE: bool = False
STRUCTURAL_STOP_BUFFER_PTS: float = 2.0

# Thesis-invalidation exits (close-based; fires before stop check).
# MSS: close beyond the divergence bar's wick extreme on the entry instrument.
# CISD: close beyond the midnight open (requires MIDNIGHT_OPEN_AS_TP = True).
# SMT: close beyond the MNQ level that defined the divergence (defended level).
# All optimizer search space: [True, False]
INVALIDATION_MSS_EXIT: bool = False
INVALIDATION_CISD_EXIT: bool = False
INVALIDATION_SMT_EXIT: bool = False

# Overnight sweep gate: require overnight H (for shorts) or L (for longs)
# to have been swept before the signal bar fires.
# OVERNIGHT_RANGE_AS_TP: use opposite overnight extreme as TP instead of TDO/midnight.
# Optimizer search space: [True, False]
OVERNIGHT_SWEEP_REQUIRED: bool = False
OVERNIGHT_RANGE_AS_TP: bool = False

# Silver Bullet window: restrict new divergence detection to 09:50–10:10 ET.
# Re-entries allowed outside window if original divergence was inside it.
# Optimizer search space: [True, False]
SILVER_BULLET_WINDOW_ONLY: bool = False
SILVER_BULLET_START = "09:50"
SILVER_BULLET_END   = "10:10"

# Hidden SMT: body/close-based divergence (MES close new session extreme,
# MNQ close does not). Only fires if wick SMT did not fire on the same bar.
# Optimizer search space: [True, False]
# Approved in Round 1 experiments: +30.6% PnL, lower drawdown, same signal quality.
HIDDEN_SMT_ENABLED: bool = True


# ── Module-level bar data ─────────────────────────────────────────────────────
_mnq_bars: "pd.DataFrame | None" = None
_mes_bars: "pd.DataFrame | None" = None


def set_bar_data(mnq_df: pd.DataFrame, mes_df: pd.DataFrame) -> None:
    """Populate module-level bar globals for strategy functions that need lookback.

    Reserved for multi-bar lookback logic (e.g. prior-session anchor, ATR filter).
    Called by run_backtest() and both 1m bar callbacks in signal_smt.
    """
    global _mnq_bars, _mes_bars
    _mnq_bars = mnq_df
    _mes_bars = mes_df


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
) -> tuple[str, float, float, str, float] | None:
    """Check for SMT divergence at bar_idx.

    Returns tuple (direction, sweep_pts, miss_pts, smt_type, smt_defended_level) or None.
    - direction: "short" if MES makes new session high but MNQ does not;
                 "long"  if MES makes new session low  but MNQ does not.
    - sweep_pts: how far MES exceeded the session extreme (always >= 0)
    - miss_pts:  how far MNQ failed to match MES (always >= 0)
    - smt_type: "wick" for high/low-based divergence; "body" for close-based (hidden SMT)
    - smt_defended_level: MNQ session extreme MNQ failed to match
    Returns None if no divergence, bar-count guard fires, or sweep/miss filters reject.

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
        smt_sweep = cur_mes["High"] - mes_session_high
        mnq_miss   = mnq_session_high - cur_mnq["High"]
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("short", smt_sweep, mnq_miss, "wick", mnq_session_high)
    # Bullish SMT: MES sweeps session low but MNQ fails to confirm
    if cur_mes["Low"] < mes_session_low and cur_mnq["Low"] >= mnq_session_low:
        smt_sweep = mes_session_low - cur_mes["Low"]
        mnq_miss   = cur_mnq["Low"] - mnq_session_low
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("long", smt_sweep, mnq_miss, "wick", mnq_session_low)

    # Hidden SMT: body/close-based divergence (fires only when wick SMT did not).
    # MES close makes new session extreme but MNQ close does not confirm.
    if HIDDEN_SMT_ENABLED:
        mes_close_session_high = mes_bars["Close"].iloc[session_slice].max()
        mnq_close_session_high = mnq_bars["Close"].iloc[session_slice].max()
        mes_close_session_low  = mes_bars["Close"].iloc[session_slice].min()
        mnq_close_session_low  = mnq_bars["Close"].iloc[session_slice].min()
        if cur_mes["Close"] > mes_close_session_high and cur_mnq["Close"] <= mnq_close_session_high:
            smt_sweep = cur_mes["Close"] - mes_close_session_high
            mnq_miss   = mnq_close_session_high - cur_mnq["Close"]
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("short", smt_sweep, mnq_miss, "body", mnq_close_session_high)
        if cur_mes["Close"] < mes_close_session_low and cur_mnq["Close"] >= mnq_close_session_low:
            smt_sweep = mes_close_session_low - cur_mes["Close"]
            mnq_miss   = cur_mnq["Close"] - mnq_close_session_low
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("long", smt_sweep, mnq_miss, "body", mnq_close_session_low)
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


def compute_midnight_open(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """Return the Open of the first 1m/5m bar at or after 00:00 ET on date.

    ICT canonical intraday reversion target. Falls back to the first bar on
    that date if no bar exists exactly at midnight (e.g. on 5m resampled data).
    Returns None if no bars exist for the date.
    """
    day_bars = mnq_bars[mnq_bars.index.date == date]
    if day_bars.empty:
        return None
    midnight = pd.Timestamp(f"{date} 00:00:00", tz="America/New_York")
    after_midnight = day_bars[day_bars.index >= midnight]
    if not after_midnight.empty:
        return float(after_midnight.iloc[0]["Open"])
    return float(day_bars.iloc[0]["Open"])


def compute_overnight_range(mnq_bars: pd.DataFrame, date: datetime.date) -> dict:
    """Return overnight high/low: bars on date with time < 09:00 ET.

    Returns {"overnight_high": float, "overnight_low": float} or
    {"overnight_high": None, "overnight_low": None} if no pre-9am bars exist.
    """
    mask = (
        (mnq_bars.index.date == date) &
        (mnq_bars.index.time < pd.Timestamp("2000-01-01 09:00:00").time())
    )
    bars = mnq_bars[mask]
    if bars.empty:
        return {"overnight_high": None, "overnight_low": None}
    return {
        "overnight_high": float(bars["High"].max()),
        "overnight_low":  float(bars["Low"].min()),
    }


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
    midnight_open=None,
    overnight_range=None,
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

        _smt = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0)
        if _smt is None:
            continue
        direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt
        if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
            continue

        # Overnight sweep gate: require overnight H (shorts) or L (longs) to have been swept.
        if OVERNIGHT_SWEEP_REQUIRED and overnight_range is not None:
            oh = overnight_range.get("overnight_high")
            ol = overnight_range.get("overnight_low")
            if direction == "short" and oh is not None:
                pre_session_high = mnq_reset["High"].iloc[:bar_idx].max() if bar_idx > 0 else 0
                if pre_session_high <= oh:
                    continue
            if direction == "long" and ol is not None:
                pre_session_low = mnq_reset["Low"].iloc[:bar_idx].min() if bar_idx > 0 else float("inf")
                if pre_session_low >= ol:
                    continue

        # Silver bullet window: only accept divergences during 09:50–10:10 ET.
        if SILVER_BULLET_WINDOW_ONLY:
            bar_t = mnq_bars.index[bar_idx].strftime("%H:%M")
            if not (SILVER_BULLET_START <= bar_t < SILVER_BULLET_END):
                continue

        _div_bar = mnq_reset.iloc[bar_idx]
        _div_bar_high = float(_div_bar["High"])
        _div_bar_low  = float(_div_bar["Low"])

        # TP target selection: overnight range → midnight open → TDO (fallback).
        if OVERNIGHT_RANGE_AS_TP and overnight_range is not None:
            _raw = overnight_range.get("overnight_low" if direction == "short" else "overnight_high")
            _tp = _raw if _raw is not None else tdo
        elif MIDNIGHT_OPEN_AS_TP and midnight_open is not None:
            _tp = midnight_open
        else:
            _tp = tdo

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
            signal = _build_signal_from_bar(
                conf_bar, entry_time, direction, _tp,
                smt_sweep_pts=_smt_sweep,
                smt_miss_pts=_smt_miss,
                divergence_bar_high=_div_bar_high,
                divergence_bar_low=_div_bar_low,
                midnight_open=midnight_open,
                smt_defended_level=_smt_defended,
                smt_type=_smt_type,
            )
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
    smt_sweep_pts: float = 0.0,
    smt_miss_pts: float = 0.0,
    divergence_bar_idx: int = -1,
    # New Plan 1 fields:
    divergence_bar_high=None,
    divergence_bar_low=None,
    midnight_open=None,
    smt_defended_level=None,
    smt_type: str = "wick",
) -> dict | None:
    """Build a signal dict from a confirmed entry bar, applying all validity guards.

    Returns None if the signal fails TDO_VALIDITY_CHECK, MIN_STOP_POINTS,
    MIN_TDO_DISTANCE_PTS, or MAX_TDO_DISTANCE_PTS guards.
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
    # Ceiling filter — trades with extreme TDO distance have collapsing RR and negative EP
    if MAX_TDO_DISTANCE_PTS < 999.0 and distance_to_tdo > MAX_TDO_DISTANCE_PTS:
        return None

    # Structural stop: beyond the divergence bar's wick extreme + buffer.
    # Falls back to ratio-based stop if divergence bar extremes are unavailable.
    if STRUCTURAL_STOP_MODE and divergence_bar_high is not None and divergence_bar_low is not None:
        if direction == "short":
            stop_price = divergence_bar_high + STRUCTURAL_STOP_BUFFER_PTS
        else:
            stop_price = divergence_bar_low - STRUCTURAL_STOP_BUFFER_PTS
    else:
        stop_ratio = SHORT_STOP_RATIO if direction == "short" else LONG_STOP_RATIO
        if direction == "short":
            stop_price = entry_price + stop_ratio * distance_to_tdo
        else:
            stop_price = entry_price - stop_ratio * distance_to_tdo

    if MIN_STOP_POINTS > 0 and abs(entry_price - stop_price) < MIN_STOP_POINTS:
        return None

    bar_range = bar["High"] - bar["Low"]
    entry_bar_body_ratio = (
        abs(bar["Close"] - bar["Open"]) / bar_range if bar_range > 0 else 0.0
    )

    return {
        "direction":            direction,
        "entry_price":          entry_price,
        "entry_time":           ts,
        "take_profit":          tdo,
        "stop_price":           round(stop_price, 4),
        "tdo":                  tdo,
        "divergence_bar":       divergence_bar_idx,
        "entry_bar":            -1,
        # Diagnostic fields — captured for analysis, no filter logic applied
        "smt_sweep_pts":        round(smt_sweep_pts, 4),
        "smt_miss_pts":         round(smt_miss_pts, 4),
        "entry_bar_body_ratio": round(entry_bar_body_ratio, 4),
        # Plan 1 structural fields
        "divergence_bar_high":  divergence_bar_high,
        "divergence_bar_low":   divergence_bar_low,
        "midnight_open":        midnight_open,
        "smt_defended_level":   smt_defended_level,
        "smt_type":             smt_type,
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

    # ── Thesis-invalidation exits (close-based; fire before stop check) ─────────
    # MSS: divergence extreme breached on a closing basis.
    if INVALIDATION_MSS_EXIT:
        div_low  = position.get("divergence_bar_low")
        div_high = position.get("divergence_bar_high")
        if direction == "long" and div_low is not None and current_bar["Close"] < div_low:
            return "exit_invalidation_mss"
        if direction == "short" and div_high is not None and current_bar["Close"] > div_high:
            return "exit_invalidation_mss"

    # CISD: midnight open breached on a closing basis.
    if INVALIDATION_CISD_EXIT:
        mo = position.get("midnight_open")
        if mo is not None:
            if direction == "long"  and current_bar["Close"] < mo:
                return "exit_invalidation_cisd"
            if direction == "short" and current_bar["Close"] > mo:
                return "exit_invalidation_cisd"

    # SMT: MNQ defended level breached on a closing basis.
    if INVALIDATION_SMT_EXIT:
        defended = position.get("smt_defended_level")
        if defended is not None:
            if direction == "long"  and current_bar["Close"] < defended:
                return "exit_invalidation_smt"
            if direction == "short" and current_bar["Close"] > defended:
                return "exit_invalidation_smt"

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
