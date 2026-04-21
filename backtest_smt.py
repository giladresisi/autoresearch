"""backtest_smt.py — SMT Divergence backtest harness. Frozen — do not modify. Run: uv run python backtest_smt.py"""
import datetime
import json
import math as _math
import os
import sys
from pathlib import Path

import pandas as pd

from hypothesis_smt import compute_hypothesis_context
from strategy_smt import (
    set_bar_data, load_futures_data, compute_tdo, find_anchor_close,
    is_confirmation_bar, detect_smt_divergence, _build_signal_from_bar,
    manage_position, print_direction_breakdown, screen_session,
    compute_midnight_open, compute_overnight_range,
    compute_pdh_pdl, select_draw_on_liquidity,
    detect_fvg, detect_displacement, detect_smt_fill,
    SESSION_START, SESSION_END, TRADE_DIRECTION, ALLOWED_WEEKDAYS,
    SIGNAL_BLACKOUT_START, SIGNAL_BLACKOUT_END, MIN_BARS_BEFORE_SIGNAL,
    REENTRY_MAX_MOVE_PTS, REENTRY_MAX_MOVE_RATIO, MIN_PRIOR_TRADE_BARS_HELD, MAX_HOLD_BARS,
    MAX_REENTRY_COUNT,
    MIDNIGHT_OPEN_AS_TP, OVERNIGHT_SWEEP_REQUIRED, OVERNIGHT_RANGE_AS_TP,
    MIN_RR_FOR_TARGET, MIN_TARGET_PTS,
    SILVER_BULLET_WINDOW_ONLY, SILVER_BULLET_START, SILVER_BULLET_END,
    TWO_LAYER_POSITION, LAYER_A_FRACTION, FVG_LAYER_B_TRIGGER,
    SMT_OPTIONAL, SMT_FILL_ENABLED, PARTIAL_EXIT_ENABLED, PARTIAL_EXIT_FRACTION,
    DISPLACEMENT_STOP_MODE, MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT,
    FVG_LAYER_B_REQUIRES_HYPOTHESIS, STRUCTURAL_STOP_BUFFER_PTS,
    PESSIMISTIC_FILLS,
    EXPANDED_REFERENCE_LEVELS, HTF_VISIBILITY_REQUIRED, HTF_PERIODS_MINUTES,
    HIDDEN_SMT_ENABLED, _compute_ref_levels, _check_smt_against_ref,
    ALWAYS_REQUIRE_CONFIRMATION,
    divergence_score, _effective_div_score,
    MIN_DIV_SCORE, REPLACE_THRESHOLD,
    DIV_SCORE_DECAY_FACTOR, DIV_SCORE_DECAY_INTERVAL,
    ADVERSE_MOVE_FULL_DECAY_PTS, ADVERSE_MOVE_MIN_DECAY,
    HYPOTHESIS_INVALIDATION_PTS,
)


class _BarRow:
    """Lightweight bar data holder — replaces pd.Series from iterrows() in the session loop.

    Supports bar["Open"], bar["High"], bar["Low"], bar["Close"] and bar.name (timestamp).
    """
    __slots__ = ("Open", "High", "Low", "Close", "name")

    def __init__(self, o: float, h: float, l: float, c: float, ts) -> None:
        self.Open  = o
        self.High  = h
        self.Low   = l
        self.Close = c
        self.name  = ts

    def __getitem__(self, key: str) -> float:
        return getattr(self, key)

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

# Set True only for the special post-loop final test run.
WRITE_FINAL_OUTPUTS = False

# Print per-direction win rate, avg PnL, and exit breakdown after each fold.
# Set False to suppress — does not affect frozen print_results output.
PRINT_DIRECTION_BREAKDOWN = True

# When True, only signals where matches_hypothesis == True are taken.
# Default False = current behaviour. Set True to test Outcome B walk-forward validation.
# Optimization search space: [True, False]
HYPOTHESIS_FILTER: bool = False

# MNQ futures P&L per point per contract.
MNQ_PNL_PER_POINT = 2.0

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

# Slippage applied to market orders (session_close, exit_time, exit_market) when
# PESSIMISTIC_FILLS is True. 5 pts is intentionally pessimistic for MNQ in RTH.
MARKET_ORDER_SLIPPAGE_PTS: float = 5.0


def _build_trade_record(
    position: dict,
    exit_result: str,
    exit_bar: "pd.Series | _BarRow",
    pnl_per_point: float,
) -> "tuple[dict, float]":
    """Build the trade dict and compute PnL from a closed position."""
    direction_sign = 1 if position["direction"] == "long" else -1
    if exit_result == "exit_tp":
        # Limit order — fills at the defined take-profit price; no slippage on liquid NQ
        exit_price = position["take_profit"]
    elif exit_result == "exit_secondary":
        # Limit order hit at secondary target — fills at defined price, identical semantics to exit_tp
        exit_price = position["secondary_target"]
    elif exit_result == "exit_stop":
        # Stop-limit order — fills at the defined stop price; no slippage on liquid NQ
        exit_price = position["stop_price"]
    else:
        # Market orders (exit_time, session_close, exit_market, end_of_backtest,
        # exit_invalidation_*) — simulate with bar mid +/- slippage
        mid = (float(exit_bar["High"]) + float(exit_bar["Low"])) / 2.0
        if PESSIMISTIC_FILLS:
            slip = MARKET_ORDER_SLIPPAGE_PTS
            direction_sign = 1 if position["direction"] == "long" else -1
            exit_price = mid - direction_sign * slip
        else:
            exit_price = mid

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
        # Quality/diagnostic fields
        "reentry_sequence":      position.get("reentry_sequence", 1),
        "prior_trade_bars_held": position.get("prior_trade_bars_held", 0),
        "entry_bar_body_ratio":  round(position.get("entry_bar_body_ratio", 0.0), 4),
        "smt_sweep_pts":         round(position.get("smt_sweep_pts", 0.0), 4),
        "smt_miss_pts":          round(position.get("smt_miss_pts", 0.0), 4),
        "bars_since_divergence": (
            position.get("entry_bar", -1) - position.get("divergence_bar", -1)
            if position.get("entry_bar", -1) >= 0 and position.get("divergence_bar", -1) >= 0
            else -1
        ),
        "smt_type": position.get("smt_type", "wick"),
        # Plan 4 fields
        "displacement_body_pts": position.get("displacement_body_pts"),
        "pessimistic_fills": PESSIMISTIC_FILLS,
        # Solution F diagnostic fields
        "tp_name":               position.get("tp_name"),
        "secondary_target_name": position.get("secondary_target_name"),
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
    set_bar_data(mnq_df, mes_df)
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

    # Quality state — reset per day
    reentry_count         = 0    # how many entries taken today
    prior_trade_bars_held = 0    # how long the previous trade lasted
    divergence_bar_idx    = -1   # bar index where divergence was detected this session
    pending_smt_sweep     = 0.0
    pending_smt_miss      = 0.0

    # Load 5m historical for hypothesis direction (deterministic, no API calls)
    _hist_mnq_path = Path("data/historical/MNQ.parquet")
    _hist_mnq_df = pd.read_parquet(_hist_mnq_path) if _hist_mnq_path.exists() else pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"]
    )

    # Precompute once — avoids 807K-row .date/.time extraction on every session day
    _mnq_dates   = mnq_df.index.date
    _mnq_times   = mnq_df.index.time
    _ses_start_t = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
    _ses_end_t   = pd.Timestamp(f"2000-01-01 {SESSION_END}").time()

    for day in trading_days:
        # Weekday filter: skip disallowed trading days (e.g. Thursday)
        if day.weekday() not in ALLOWED_WEEKDAYS:
            continue

        session_mask = (
            (_mnq_dates == day)
            & (_mnq_times >= _ses_start_t)
            & (_mnq_times <= _ses_end_t)
        )
        mnq_session = mnq_df[session_mask]
        mes_session = mes_df[session_mask]

        if mnq_session.empty:
            equity_curve.append(equity_curve[-1])
            continue

        session_end_ts = pd.Timestamp(
            f"{day} {SESSION_END}", tz=mnq_session.index.tz
        )
        day_pnl = 0.0

        # Compute TDO for new signal generation; skip the day only if TDO is
        # missing AND we are not currently managing a carried position.
        mnq_day = mnq_df[_mnq_dates == day]
        day_tdo = compute_tdo(mnq_day, day)
        if day_tdo is None and state != "IN_TRADE":
            equity_curve.append(equity_curve[-1])
            continue

        _day_midnight_open = compute_midnight_open(mnq_df, day) if MIDNIGHT_OPEN_AS_TP else None
        _day_overnight = (
            compute_overnight_range(mnq_df, day)
            if (OVERNIGHT_SWEEP_REQUIRED or OVERNIGHT_RANGE_AS_TP)
            else {"overnight_high": None, "overnight_low": None}
        )
        _day_pdh, _day_pdl = compute_pdh_pdl(_hist_mnq_df, day)

        # Hypothesis direction + per-rule context for this session (deterministic, no LLM)
        _session_hyp_ctx = compute_hypothesis_context(mnq_df, _hist_mnq_df, day)
        _session_hyp_dir = _session_hyp_ctx["direction"] if _session_hyp_ctx else None

        # Reset pending state at day boundary — divergence signals are session-scoped.
        # An open position (IN_TRADE) is allowed to carry across days.
        if state != "IN_TRADE":
            state = "IDLE"
            pending_direction = None
            anchor_close = None
            reentry_count         = 0
            prior_trade_bars_held = 0
            divergence_bar_idx    = -1
            pending_smt_sweep     = 0.0
            pending_smt_miss      = 0.0
            _pending_div_bar_high = 0.0
            _pending_div_bar_low  = 0.0
            _pending_smt_defended = 0.0
            _pending_smt_type               = "wick"
            _pending_fvg_zone               = None
            _pending_fvg_detected           = False
            _pending_displacement_bar_extreme = None
            _pending_div_score           = 0.0
            _pending_div_provisional     = False
            _pending_discovery_bar_idx   = -1
            _pending_discovery_price     = 0.0

        min_signal_ts = mnq_session.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)

        # Pre-compute reset-index views once per session to avoid repeated resets in the loop.
        mes_reset = mes_session.reset_index(drop=True)
        mnq_reset = mnq_session.reset_index(drop=True)

        # Pre-extract numpy arrays for fast per-bar access (avoids iterrows() Series overhead)
        _mnq_opens  = mnq_session["Open"].values
        _mnq_highs  = mnq_session["High"].values
        _mnq_lows   = mnq_session["Low"].values
        _mnq_closes = mnq_session["Close"].values
        _mes_highs  = mes_session["High"].values
        _mes_lows   = mes_session["Low"].values
        _mes_closes = mes_session["Close"].values
        _mnq_idx    = mnq_session.index

        # Prev-day slices for EXPANDED_REFERENCE_LEVELS
        _prev_day = day - pd.Timedelta(days=1) if hasattr(day, "days") else \
            day.__class__.fromordinal(day.toordinal() - 1)
        _prev_day_date = _prev_day if hasattr(_prev_day, "weekday") else _prev_day
        _prev_day_mnq = mnq_df[_mnq_dates == _prev_day_date] if EXPANDED_REFERENCE_LEVELS else None
        _prev_day_mes = mes_df[mes_df.index.date == _prev_day_date] if EXPANDED_REFERENCE_LEVELS else None
        # Prev session: same day's session bars (already computed each day) - use prev trading day
        _prev_ses_mnq: "pd.DataFrame | None" = None
        _prev_ses_mes: "pd.DataFrame | None" = None
        if EXPANDED_REFERENCE_LEVELS and _prev_day_mnq is not None and not _prev_day_mnq.empty:
            _ses_start_prev = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
            _ses_end_prev   = pd.Timestamp(f"2000-01-01 {SESSION_END}").time()
            _prev_ses_mask  = (
                (_prev_day_mnq.index.time >= _ses_start_prev)
                & (_prev_day_mnq.index.time <= _ses_end_prev)
            )
            _prev_ses_mnq = _prev_day_mnq[_prev_ses_mask]
            _prev_ses_mes = _prev_day_mes[
                (_prev_day_mes.index.time >= _ses_start_prev)
                & (_prev_day_mes.index.time <= _ses_end_prev)
            ] if _prev_day_mes is not None and not _prev_day_mes.empty else None

        # Reference level cache for EXPANDED_REFERENCE_LEVELS
        _bt_ref_lvls: dict = {}
        if EXPANDED_REFERENCE_LEVELS:
            # Current calendar week bars (Mon–current day) for week H/L reference level.
            import datetime as _dt
            _week_start = day - _dt.timedelta(days=day.weekday())
            _week_mnq = mnq_df[(_mnq_dates >= _week_start) & (_mnq_dates < day)] \
                if _week_start < day else None
            _week_mes = mes_df[
                (mes_df.index.date >= _week_start) & (mes_df.index.date < day)
            ] if _week_start < day else None
            _bt_ref_lvls = _compute_ref_levels(
                _prev_day_mnq, _prev_day_mes, _prev_ses_mnq, _prev_ses_mes,
                _week_mnq, _week_mes,
            )

        # HTF state for HTF_VISIBILITY_REQUIRED (same pattern as screen_session)
        _bt_htf_state: dict = {}
        if HTF_VISIBILITY_REQUIRED:
            import math as _math_htf
            for _T in HTF_PERIODS_MINUTES:
                _bt_htf_state[_T] = {
                    "pstart": None,
                    "c_mes_h": float("nan"), "c_mes_l": float("nan"),
                    "c_mnq_h": float("nan"), "c_mnq_l": float("nan"),
                    "c_mes_ch": float("nan"), "c_mes_cl": float("nan"),
                    "c_mnq_ch": float("nan"), "c_mnq_cl": float("nan"),
                    "p_mes_h": None, "p_mes_l": None,
                    "p_mnq_h": None, "p_mnq_l": None,
                    "p_mes_ch": None, "p_mes_cl": None,
                    "p_mnq_ch": None, "p_mnq_cl": None,
                }

        # Running session extremes — updated at start of each bar for bars [0..bar_idx-1]
        # Initialized to nan so bar 0 comparisons behave identically to empty-slice .max()/.min()
        _ses_mes_h  = _ses_mes_l  = float("nan")
        _ses_mnq_h  = _ses_mnq_l  = float("nan")
        _ses_mes_ch = _ses_mes_cl = float("nan")
        _ses_mnq_ch = _ses_mnq_cl = float("nan")
        # Running high/low for overnight sweep gate (replaces slice-based recomputation)
        _run_ses_high = -float("inf")  # symmetric with _run_ses_low; guard (_run_ses_high > _ep+1) filters it safely
        _run_ses_low  = float("inf")

        for bar_idx in range(len(mnq_session)):
            ts = _mnq_idx[bar_idx]

            # Bring running extremes up-to-date with the previous bar.
            # Done at the TOP so continue-statements elsewhere cannot skip the update.
            if bar_idx > 0:
                _p = bar_idx - 1
                _v = float(_mes_highs[_p])
                _ses_mes_h   = _v if _math.isnan(_ses_mes_h)  else max(_ses_mes_h,  _v)
                _v = float(_mes_lows[_p])
                _ses_mes_l   = _v if _math.isnan(_ses_mes_l)  else min(_ses_mes_l,  _v)
                _v = float(_mnq_highs[_p])
                _ses_mnq_h   = _v if _math.isnan(_ses_mnq_h)  else max(_ses_mnq_h,  _v)
                _run_ses_high = max(_run_ses_high, _v)
                _v = float(_mnq_lows[_p])
                _ses_mnq_l   = _v if _math.isnan(_ses_mnq_l)  else min(_ses_mnq_l,  _v)
                _run_ses_low  = min(_run_ses_low,  _v)
                _v = float(_mes_closes[_p])
                _ses_mes_ch  = _v if _math.isnan(_ses_mes_ch) else max(_ses_mes_ch, _v)
                _ses_mes_cl  = _v if _math.isnan(_ses_mes_cl) else min(_ses_mes_cl, _v)
                _v = float(_mnq_closes[_p])
                _ses_mnq_ch  = _v if _math.isnan(_ses_mnq_ch) else max(_ses_mnq_ch, _v)
                _ses_mnq_cl  = _v if _math.isnan(_ses_mnq_cl) else min(_ses_mnq_cl, _v)

            _smt_cache = {
                "mes_h":  _ses_mes_h,  "mes_l":  _ses_mes_l,
                "mnq_h":  _ses_mnq_h,  "mnq_l":  _ses_mnq_l,
                "mes_ch": _ses_mes_ch, "mes_cl": _ses_mes_cl,
                "mnq_ch": _ses_mnq_ch, "mnq_cl": _ses_mnq_cl,
            }

            # HTF period extreme update for HTF_VISIBILITY_REQUIRED
            if HTF_VISIBILITY_REQUIRED and _bt_htf_state:
                _ts_ns = _mnq_idx[bar_idx].value
                for _T, _hs in _bt_htf_state.items():
                    _period_ns = _T * 60 * 10**9
                    _pstart = (_ts_ns // _period_ns) * _period_ns
                    if _hs["pstart"] != _pstart:
                        if _hs["pstart"] is not None and not _math.isnan(_hs["c_mes_h"]):
                            _hs["p_mes_h"]  = _hs["c_mes_h"];  _hs["p_mes_l"]  = _hs["c_mes_l"]
                            _hs["p_mnq_h"]  = _hs["c_mnq_h"];  _hs["p_mnq_l"]  = _hs["c_mnq_l"]
                            _hs["p_mes_ch"] = _hs["c_mes_ch"]; _hs["p_mes_cl"] = _hs["c_mes_cl"]
                            _hs["p_mnq_ch"] = _hs["c_mnq_ch"]; _hs["p_mnq_cl"] = _hs["c_mnq_cl"]
                        _hs["pstart"] = _pstart
                        _hs["c_mes_h"] = float(_mes_highs[bar_idx])
                        _hs["c_mes_l"] = float(_mes_lows[bar_idx])
                        _hs["c_mnq_h"] = float(_mnq_highs[bar_idx])
                        _hs["c_mnq_l"] = float(_mnq_lows[bar_idx])
                        _hs["c_mes_ch"] = float(_mes_closes[bar_idx])
                        _hs["c_mes_cl"] = float(_mes_closes[bar_idx])
                        _hs["c_mnq_ch"] = float(_mnq_closes[bar_idx])
                        _hs["c_mnq_cl"] = float(_mnq_closes[bar_idx])
                    else:
                        _hs["c_mes_h"]  = max(_hs["c_mes_h"],  float(_mes_highs[bar_idx]))
                        _hs["c_mes_l"]  = min(_hs["c_mes_l"],  float(_mes_lows[bar_idx]))
                        _hs["c_mnq_h"]  = max(_hs["c_mnq_h"],  float(_mnq_highs[bar_idx]))
                        _hs["c_mnq_l"]  = min(_hs["c_mnq_l"],  float(_mnq_lows[bar_idx]))
                        _hs["c_mes_ch"] = max(_hs["c_mes_ch"], float(_mes_closes[bar_idx]))
                        _hs["c_mes_cl"] = min(_hs["c_mes_cl"], float(_mes_closes[bar_idx]))
                        _hs["c_mnq_ch"] = max(_hs["c_mnq_ch"], float(_mnq_closes[bar_idx]))
                        _hs["c_mnq_cl"] = min(_hs["c_mnq_cl"], float(_mnq_closes[bar_idx]))

            bar = _BarRow(
                float(_mnq_opens[bar_idx]),
                float(_mnq_highs[bar_idx]),
                float(_mnq_lows[bar_idx]),
                float(_mnq_closes[bar_idx]),
                ts,
            )

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

                if result == "partial_exit":
                    # Clamp so at least 1 contract always remains after the partial
                    partial_contracts = min(
                        max(1, int(position["contracts"] * PARTIAL_EXIT_FRACTION)),
                        position["contracts"] - 1,
                    )
                    if partial_contracts < 1:
                        # Single-contract position: partial is impossible; skip silently.
                        # partial_done is already True so this branch won't re-trigger.
                        continue
                    partial_exit_price = position.get("partial_price", float(bar["Close"]))
                    pnl_per_contract = (
                        (partial_exit_price - position["entry_price"]) if position["direction"] == "long"
                        else (position["entry_price"] - partial_exit_price)
                    )
                    partial_pnl = pnl_per_contract * partial_contracts * MNQ_PNL_PER_POINT
                    partial_trade, _ = _build_trade_record(
                        {**position, "contracts": partial_contracts}, "partial_exit", bar, MNQ_PNL_PER_POINT
                    )
                    partial_trade["matches_hypothesis"] = position.get("matches_hypothesis")
                    for _f in ("hypothesis_direction", "pd_range_case", "pd_range_bias",
                               "week_zone", "day_zone", "trend_direction", "hypothesis_score",
                               "fvg_detected"):
                        partial_trade[_f] = position.get(_f)
                    partial_trade["exit_type"] = "partial_exit"
                    partial_trade["pnl"] = round(partial_pnl, 2)
                    partial_trade["exit_price"] = round(partial_exit_price, 4)
                    trades.append(partial_trade)
                    day_pnl += partial_pnl
                    position["contracts"] -= partial_contracts
                    # Stay IN_TRADE with remaining contracts
                    continue

                if result != "hold":
                    trade, day_pnl_delta = _build_trade_record(
                        position, result, bar, MNQ_PNL_PER_POINT
                    )
                    trade["matches_hypothesis"] = position.get("matches_hypothesis")
                    for _f in ("hypothesis_direction", "pd_range_case", "pd_range_bias",
                               "week_zone", "day_zone", "trend_direction", "hypothesis_score",
                               "fvg_detected"):
                        trade[_f] = position.get(_f)
                    trades.append(trade)
                    day_pnl += day_pnl_delta
                    prior_trade_bars_held = entry_bar_count  # capture before reset

                    # Determine re-entry eligibility after a stop-out or time exit.
                    if result in ("exit_stop", "exit_time"):
                        if position.get("breakeven_active"):
                            # Stop was at breakeven — price never really moved; always eligible.
                            state = "REENTRY_ELIGIBLE"
                            anchor_close = float(bar["Close"])
                        else:
                            if position["direction"] == "short":
                                move = position["entry_price"] - float(bar["Close"])
                            else:
                                move = float(bar["Close"]) - position["entry_price"]
                            # REENTRY_MAX_MOVE_PTS=0 is the disable sentinel — skip entirely.
                            # When enabled, combined threshold = min(absolute pts, ratio-based).
                            # REENTRY_MAX_MOVE_PTS=999 defers to ratio only.
                            entry_to_tp = abs(position["entry_price"] - position["tdo"])
                            ratio_threshold = REENTRY_MAX_MOVE_RATIO * entry_to_tp if REENTRY_MAX_MOVE_RATIO < 99 else 9999
                            move_threshold = min(REENTRY_MAX_MOVE_PTS, ratio_threshold) if REENTRY_MAX_MOVE_PTS > 0 else -1
                            if REENTRY_MAX_MOVE_PTS > 0 and move < move_threshold:
                                # Require prior trade to have lasted long enough (diagnostic filter; default 0 = disabled)
                                if MIN_PRIOR_TRADE_BARS_HELD > 0 and prior_trade_bars_held < MIN_PRIOR_TRADE_BARS_HELD:
                                    state = "IDLE"
                                else:
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
                # Replacement check: scan for a new divergence that could displace the pending hypothesis
                _new_div = detect_smt_divergence(
                    mes_reset,
                    mnq_reset,
                    bar_idx,
                    0,
                    _cached=_smt_cache,
                )
                if _new_div is not None:
                    _nd_dir, _nd_sweep, _nd_miss, _nd_type, _nd_defended = _new_div
                    _nd_body = abs(float(bar["Close"]) - float(bar["Open"]))
                    _nd_score = divergence_score(
                        _nd_sweep, _nd_miss, _nd_body,
                        _nd_type, _session_hyp_dir, _nd_dir,
                    )
                    if _nd_score >= MIN_DIV_SCORE:
                        _eff = _effective_div_score(
                            _pending_div_score, _pending_discovery_bar_idx, bar_idx,
                            _pending_discovery_price, pending_direction,
                            float(bar["High"]), float(bar["Low"]),
                        )
                        _replace = False
                        # Rule 1: displacement can never displace wick/body
                        if _pending_smt_type in ("wick", "body") and _nd_type == "displacement":
                            _replace = False
                        # Rule 2: any wick/body replaces a provisional (displacement) pending
                        elif _pending_div_provisional and _nd_type in ("wick", "body"):
                            _replace = True
                        # Rule 3: same direction — upgrade if strictly stronger
                        elif _nd_dir == pending_direction and _nd_score > _eff:
                            _replace = True
                        # Rule 4: opposite direction — replace if significantly stronger
                        elif _nd_dir != pending_direction and _nd_score > _eff * REPLACE_THRESHOLD:
                            _replace = True

                        if _replace:
                            _new_ac = find_anchor_close(mnq_reset, bar_idx, _nd_dir)
                            if _new_ac is not None:
                                pending_direction           = _nd_dir
                                anchor_close                = _new_ac
                                pending_smt_sweep           = _nd_sweep
                                pending_smt_miss            = _nd_miss
                                _pending_div_bar_high       = float(bar["High"])
                                _pending_div_bar_low        = float(bar["Low"])
                                _pending_smt_defended       = _nd_defended
                                _pending_smt_type           = _nd_type
                                _pending_div_score          = _nd_score
                                _pending_div_provisional    = (_nd_type == "displacement")
                                _pending_discovery_bar_idx  = bar_idx
                                _pending_discovery_price    = float(bar["Close"])
                                divergence_bar_idx          = bar_idx
                                _pending_displacement_bar_extreme = (
                                    float(bar["Low"]) if _nd_dir == "long" else float(bar["High"])
                                ) if _nd_type == "displacement" else None
                                _new_fvg = detect_fvg(mnq_reset, bar_idx, _nd_dir)
                                _pending_fvg_zone     = _new_fvg
                                _pending_fvg_detected = _new_fvg is not None
                # Solution D: abandon hypothesis if adverse move exceeds threshold
                if HYPOTHESIS_INVALIDATION_PTS < 999:
                    if pending_direction == "short":
                        _adverse = float(bar["High"]) - _pending_discovery_price
                    else:
                        _adverse = _pending_discovery_price - float(bar["Low"])
                    if _adverse > HYPOTHESIS_INVALIDATION_PTS:
                        state = "IDLE"
                        pending_direction = None
                        anchor_close = None
                        continue
                if anchor_close is not None and is_confirmation_bar(bar, anchor_close, pending_direction):
                    # ALWAYS_REQUIRE_CONFIRMATION: bar must break the divergence bar's body boundary.
                    if ALWAYS_REQUIRE_CONFIRMATION and divergence_bar_idx >= 0:
                        _div = mnq_reset.iloc[divergence_bar_idx]
                        _dbh = max(float(_div["Open"]), float(_div["Close"]))
                        _dbl = min(float(_div["Open"]), float(_div["Close"]))
                        if pending_direction == "short" and float(bar["Close"]) >= _dbl:
                            continue
                        if pending_direction == "long" and float(bar["Close"]) <= _dbh:
                            continue
                    # Build signal first with TDO as placeholder TP, then select draw.
                    signal = _build_signal_from_bar(
                        bar, ts, pending_direction, day_tdo,
                        smt_sweep_pts=pending_smt_sweep,
                        smt_miss_pts=pending_smt_miss,
                        divergence_bar_idx=divergence_bar_idx,
                        divergence_bar_high=_pending_div_bar_high,
                        divergence_bar_low=_pending_div_bar_low,
                        midnight_open=_day_midnight_open,
                        smt_defended_level=_pending_smt_defended,
                        smt_type=_pending_smt_type,
                        fvg_zone=_pending_fvg_zone,
                    )
                    if signal is not None:
                        # Draw-on-liquidity target selection replaces TP cascade.
                        _ep = signal["entry_price"]
                        _sp = signal["stop_price"]
                        if pending_direction == "long":
                            _draws = {
                                "fvg_top":        _pending_fvg_zone["fvg_high"] if _pending_fvg_zone else None,
                                "tdo":            day_tdo if day_tdo and day_tdo > _ep else None,
                                "midnight_open":  _day_midnight_open if _day_midnight_open and _day_midnight_open > _ep else None,
                                "session_high":   _run_ses_high if _run_ses_high > _ep + 1 else None,
                                "overnight_high": _day_overnight.get("overnight_high") if _day_overnight.get("overnight_high") and _day_overnight.get("overnight_high") > _ep else None,
                                "pdh":            _day_pdh if _day_pdh and _day_pdh > _ep else None,
                            }
                        else:
                            _draws = {
                                "fvg_bottom":    _pending_fvg_zone["fvg_low"] if _pending_fvg_zone else None,
                                "tdo":           day_tdo if day_tdo and day_tdo < _ep else None,
                                "midnight_open": _day_midnight_open if _day_midnight_open and _day_midnight_open < _ep else None,
                                "session_low":   _run_ses_low if _run_ses_low < _ep - 1 else None,
                                "overnight_low": _day_overnight.get("overnight_low") if _day_overnight.get("overnight_low") and _day_overnight.get("overnight_low") < _ep else None,
                                "pdl":           _day_pdl if _day_pdl and _day_pdl < _ep else None,
                            }
                        _tp_name, _day_tp, _sec_tp_name, _sec_tp = select_draw_on_liquidity(
                            pending_direction, _ep, _sp, _draws, MIN_RR_FOR_TARGET, MIN_TARGET_PTS,
                        )
                        if _day_tp is None:
                            continue  # no viable draw — skip this confirmation bar
                        signal["take_profit"]  = _day_tp
                        signal["tp_name"]      = _tp_name
                        signal["matches_hypothesis"] = (
                            (signal.get("direction") == _session_hyp_dir)
                            if _session_hyp_dir is not None else None
                        )
                        signal["fvg_detected"] = _pending_fvg_detected
                        if _session_hyp_ctx is not None:
                            signal["hypothesis_direction"] = _session_hyp_ctx.get("direction")
                            signal["pd_range_case"]        = _session_hyp_ctx.get("pd_range_case")
                            signal["pd_range_bias"]        = _session_hyp_ctx.get("pd_range_bias")
                            signal["week_zone"]            = _session_hyp_ctx.get("week_zone")
                            signal["day_zone"]             = _session_hyp_ctx.get("day_zone")
                            signal["trend_direction"]      = _session_hyp_ctx.get("trend_direction")
                            signal["hypothesis_score"]     = _session_hyp_ctx.get("hypothesis_score", 0)
                        else:
                            signal["hypothesis_direction"] = None
                            signal["pd_range_case"]        = None
                            signal["pd_range_bias"]        = None
                            signal["week_zone"]            = None
                            signal["day_zone"]             = None
                            signal["trend_direction"]      = None
                            signal["hypothesis_score"]     = 0
                        if HYPOTHESIS_FILTER and signal.get("matches_hypothesis") is not True:
                            state = "IDLE"
                            pending_direction = None
                            anchor_close = None
                            continue
                        risk_per_contract = (
                            abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                        )
                        contracts = (
                            min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
                            if risk_per_contract > 0 else 1
                        )
                        total_contracts_target = contracts
                        if TWO_LAYER_POSITION:
                            layer_a_contracts = max(1, int(contracts * LAYER_A_FRACTION))
                            total_contracts_target = contracts
                            contracts = layer_a_contracts
                        position = {
                            **signal, "entry_date": day, "contracts": contracts,
                            "total_contracts_target": total_contracts_target,
                            "layer_b_entered": False, "layer_b_entry_price": None,
                            "layer_b_contracts": 0, "partial_done": False, "partial_price": None,
                        }
                        if DISPLACEMENT_STOP_MODE and position.get("smt_type") == "displacement":
                            _extreme = _pending_displacement_bar_extreme
                            if _extreme is not None:
                                if position["direction"] == "long":
                                    position["stop_price"] = _extreme - STRUCTURAL_STOP_BUFFER_PTS
                                else:
                                    position["stop_price"] = _extreme + STRUCTURAL_STOP_BUFFER_PTS
                        position["secondary_target"]      = _sec_tp
                        position["secondary_target_name"] = _sec_tp_name
                        position["tp_breached"]           = False
                        position["entry_bar"]             = bar_idx
                        reentry_count += 1
                        position["reentry_sequence"]      = reentry_count
                        position["prior_trade_bars_held"] = prior_trade_bars_held
                        state = "IN_TRADE"
                        entry_bar_count = 0

            elif state == "REENTRY_ELIGIBLE":
                # Gate: exceeded daily re-entry cap → abandon this signal
                if MAX_REENTRY_COUNT < 999 and reentry_count >= MAX_REENTRY_COUNT:
                    state = "IDLE"
                    pending_direction = None
                    anchor_close = None
                    continue
                # Apply blackout to re-entry bar, same as initial entry.
                if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
                    t = ts.strftime("%H:%M")
                    if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                        continue
                if anchor_close is not None and is_confirmation_bar(bar, anchor_close, pending_direction):
                    # ALWAYS_REQUIRE_CONFIRMATION: bar must break the divergence bar's body boundary.
                    if ALWAYS_REQUIRE_CONFIRMATION and divergence_bar_idx >= 0:
                        _div = mnq_reset.iloc[divergence_bar_idx]
                        _dbh = max(float(_div["Open"]), float(_div["Close"]))
                        _dbl = min(float(_div["Open"]), float(_div["Close"]))
                        if pending_direction == "short" and float(bar["Close"]) >= _dbl:
                            continue
                        if pending_direction == "long" and float(bar["Close"]) <= _dbh:
                            continue
                    # Build signal first with TDO as placeholder TP, then select draw.
                    signal = _build_signal_from_bar(
                        bar, ts, pending_direction, day_tdo,
                        smt_sweep_pts=pending_smt_sweep,
                        smt_miss_pts=pending_smt_miss,
                        divergence_bar_idx=divergence_bar_idx,
                        divergence_bar_high=_pending_div_bar_high,
                        divergence_bar_low=_pending_div_bar_low,
                        midnight_open=_day_midnight_open,
                        smt_defended_level=_pending_smt_defended,
                        smt_type=_pending_smt_type,
                        fvg_zone=_pending_fvg_zone,
                    )
                    if signal is not None:
                        # Draw-on-liquidity target selection replaces TP cascade.
                        _ep = signal["entry_price"]
                        _sp = signal["stop_price"]
                        if pending_direction == "long":
                            _draws = {
                                "fvg_top":        _pending_fvg_zone["fvg_high"] if _pending_fvg_zone else None,
                                "tdo":            day_tdo if day_tdo and day_tdo > _ep else None,
                                "midnight_open":  _day_midnight_open if _day_midnight_open and _day_midnight_open > _ep else None,
                                "session_high":   _run_ses_high if _run_ses_high > _ep + 1 else None,
                                "overnight_high": _day_overnight.get("overnight_high") if _day_overnight.get("overnight_high") and _day_overnight.get("overnight_high") > _ep else None,
                                "pdh":            _day_pdh if _day_pdh and _day_pdh > _ep else None,
                            }
                        else:
                            _draws = {
                                "fvg_bottom":    _pending_fvg_zone["fvg_low"] if _pending_fvg_zone else None,
                                "tdo":           day_tdo if day_tdo and day_tdo < _ep else None,
                                "midnight_open": _day_midnight_open if _day_midnight_open and _day_midnight_open < _ep else None,
                                "session_low":   _run_ses_low if _run_ses_low < _ep - 1 else None,
                                "overnight_low": _day_overnight.get("overnight_low") if _day_overnight.get("overnight_low") and _day_overnight.get("overnight_low") < _ep else None,
                                "pdl":           _day_pdl if _day_pdl and _day_pdl < _ep else None,
                            }
                        _tp_name, _day_tp, _sec_tp_name, _sec_tp = select_draw_on_liquidity(
                            pending_direction, _ep, _sp, _draws, MIN_RR_FOR_TARGET, MIN_TARGET_PTS,
                        )
                        if _day_tp is None:
                            continue  # no viable draw — skip this re-entry bar
                        signal["take_profit"]  = _day_tp
                        signal["tp_name"]      = _tp_name
                        signal["matches_hypothesis"] = (
                            (signal.get("direction") == _session_hyp_dir)
                            if _session_hyp_dir is not None else None
                        )
                        signal["fvg_detected"] = _pending_fvg_detected
                        if _session_hyp_ctx is not None:
                            signal["hypothesis_direction"] = _session_hyp_ctx.get("direction")
                            signal["pd_range_case"]        = _session_hyp_ctx.get("pd_range_case")
                            signal["pd_range_bias"]        = _session_hyp_ctx.get("pd_range_bias")
                            signal["week_zone"]            = _session_hyp_ctx.get("week_zone")
                            signal["day_zone"]             = _session_hyp_ctx.get("day_zone")
                            signal["trend_direction"]      = _session_hyp_ctx.get("trend_direction")
                            signal["hypothesis_score"]     = _session_hyp_ctx.get("hypothesis_score", 0)
                        else:
                            signal["hypothesis_direction"] = None
                            signal["pd_range_case"]        = None
                            signal["pd_range_bias"]        = None
                            signal["week_zone"]            = None
                            signal["day_zone"]             = None
                            signal["trend_direction"]      = None
                            signal["hypothesis_score"]     = 0
                        if HYPOTHESIS_FILTER and signal.get("matches_hypothesis") is not True:
                            state = "IDLE"
                            pending_direction = None
                            anchor_close = None
                            continue
                        risk_per_contract = (
                            abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                        )
                        contracts = (
                            min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
                            if risk_per_contract > 0 else 1
                        )
                        total_contracts_target = contracts
                        if TWO_LAYER_POSITION:
                            layer_a_contracts = max(1, int(contracts * LAYER_A_FRACTION))
                            total_contracts_target = contracts
                            contracts = layer_a_contracts
                        position = {
                            **signal, "entry_date": day, "contracts": contracts,
                            "total_contracts_target": total_contracts_target,
                            "layer_b_entered": False, "layer_b_entry_price": None,
                            "layer_b_contracts": 0, "partial_done": False, "partial_price": None,
                        }
                        if DISPLACEMENT_STOP_MODE and position.get("smt_type") == "displacement":
                            _extreme = _pending_displacement_bar_extreme
                            if _extreme is not None:
                                if position["direction"] == "long":
                                    position["stop_price"] = _extreme - STRUCTURAL_STOP_BUFFER_PTS
                                else:
                                    position["stop_price"] = _extreme + STRUCTURAL_STOP_BUFFER_PTS
                        position["secondary_target"]      = _sec_tp
                        position["secondary_target_name"] = _sec_tp_name
                        position["tp_breached"]           = False
                        position["entry_bar"]             = bar_idx
                        reentry_count += 1
                        position["reentry_sequence"]      = reentry_count
                        position["prior_trade_bars_held"] = prior_trade_bars_held
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

                # Solution E: skip first 3 bars — session extremes undefined with <4 data points
                if bar_idx < 3:
                    continue

                _smt = detect_smt_divergence(
                    mes_reset,
                    mnq_reset,
                    bar_idx,
                    0,
                    _cached=_smt_cache,
                )

                # Expanded reference levels: check prev-day/prev-session extremes
                if _smt is None and EXPANDED_REFERENCE_LEVELS and _bt_ref_lvls:
                    _cur_mes_b = mes_reset.iloc[bar_idx]
                    _cur_mnq_b = mnq_reset.iloc[bar_idx]
                    _best_ref_bt = None
                    for _rk in (
                        ("prev_day_mes_high", "prev_day_mes_low", "prev_day_mnq_high", "prev_day_mnq_low", False),
                        ("prev_session_mes_high", "prev_session_mes_low", "prev_session_mnq_high", "prev_session_mnq_low", False),
                        ("week_mes_high", "week_mes_low", "week_mnq_high", "week_mnq_low", False),
                        ("prev_day_mes_close_high", "prev_day_mes_close_low", "prev_day_mnq_close_high", "prev_day_mnq_close_low", True),
                        ("prev_session_mes_close_high", "prev_session_mes_close_low", "prev_session_mnq_close_high", "prev_session_mnq_close_low", True),
                        ("week_mes_close_high", "week_mes_close_low", "week_mnq_close_high", "week_mnq_close_low", True),
                    ):
                        _mh, _ml, _nh, _nl, _uc = _rk
                        if _uc and not HIDDEN_SMT_ENABLED:
                            continue
                        _cand = _check_smt_against_ref(
                            _cur_mes_b, _cur_mnq_b,
                            _bt_ref_lvls.get(_mh), _bt_ref_lvls.get(_ml),
                            _bt_ref_lvls.get(_nh), _bt_ref_lvls.get(_nl),
                            use_close=_uc,
                        )
                        if _cand is not None and (_best_ref_bt is None or _cand[1] > _best_ref_bt[1]):
                            _best_ref_bt = _cand
                    if _best_ref_bt is not None:
                        _smt = _best_ref_bt

                _smt_fill = None
                _displacement_dir = None
                if _smt is None:
                    if SMT_FILL_ENABLED:
                        _smt_fill = detect_smt_fill(mes_reset, mnq_reset, bar_idx)
                    if _smt_fill is None and SMT_OPTIONAL:
                        for _d in ("short", "long"):
                            if TRADE_DIRECTION in ("both", _d) and detect_displacement(mnq_reset, bar_idx, _d):
                                _displacement_dir = _d
                                break
                if _smt is None and _smt_fill is None and _displacement_dir is None:
                    continue

                # Resolve effective direction + aux fields
                if _smt is not None:
                    direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt
                elif _smt_fill is not None:
                    direction, _, _ = _smt_fill
                    _smt_sweep = _smt_miss = 0.0; _smt_type = "fill"; _smt_defended = None
                else:
                    direction = _displacement_dir
                    _smt_sweep = _smt_miss = 0.0; _smt_type = "displacement"; _smt_defended = None

                if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
                    continue

                # HTF visibility filter: skip if signal not visible on any configured HTF period
                if HTF_VISIBILITY_REQUIRED and _bt_htf_state:
                    _htf_ok = False
                    for _T, _hs in _bt_htf_state.items():
                        if _hs["p_mes_h"] is None:
                            continue
                        if direction == "short":
                            if _hs["c_mes_h"] > _hs["p_mes_h"] and _hs["c_mnq_h"] <= _hs["p_mnq_h"]:
                                _htf_ok = True; break
                            if HIDDEN_SMT_ENABLED and _hs["c_mes_ch"] > _hs["p_mes_ch"] and _hs["c_mnq_ch"] <= _hs["p_mnq_ch"]:
                                _htf_ok = True; break
                        else:
                            if _hs["c_mes_l"] < _hs["p_mes_l"] and _hs["c_mnq_l"] >= _hs["p_mnq_l"]:
                                _htf_ok = True; break
                            if HIDDEN_SMT_ENABLED and _hs["c_mes_cl"] < _hs["p_mes_cl"] and _hs["c_mnq_cl"] >= _hs["p_mnq_cl"]:
                                _htf_ok = True; break
                    if not _htf_ok:
                        continue

                # Stash displacement bar extreme for stop override (DISPLACEMENT_STOP_MODE)
                _displacement_bar_extreme = None
                if _smt_type == "displacement":
                    if direction == "long":
                        _displacement_bar_extreme = float(_mnq_lows[bar_idx])
                    else:
                        _displacement_bar_extreme = float(_mnq_highs[bar_idx])

                # Score gate: reject displacement entries below hypothesis alignment threshold
                if _smt_type == "displacement" and MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT > 0:
                    _score = _session_hyp_ctx.get("hypothesis_score", 0) if _session_hyp_ctx else 0
                    if _score < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT:
                        continue

                # Compute FVG zone after direction is resolved
                _pending_fvg = detect_fvg(mnq_reset, bar_idx, direction)
                _raw_fvg_present = _pending_fvg is not None  # capture before gate may clear it

                # Clear FVG zone when Layer B hypothesis gate is enabled and score is insufficient
                if FVG_LAYER_B_REQUIRES_HYPOTHESIS:
                    _score = _session_hyp_ctx.get("hypothesis_score", 0) if _session_hyp_ctx else 0
                    if _score < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT:
                        _pending_fvg = None  # Layer B disabled this session; Layer A proceeds

                # Overnight sweep gate: overnight H (shorts) or L (longs) must be exceeded.
                if OVERNIGHT_SWEEP_REQUIRED:
                    oh = _day_overnight.get("overnight_high")
                    ol = _day_overnight.get("overnight_low")
                    if direction == "short" and oh is not None:
                        if _run_ses_high <= oh:
                            continue
                    if direction == "long" and ol is not None:
                        if _run_ses_low >= ol:
                            continue

                # Silver bullet window: only accept divergences during 09:50–10:10 ET.
                if SILVER_BULLET_WINDOW_ONLY:
                    bar_t = ts.strftime("%H:%M")
                    if not (SILVER_BULLET_START <= bar_t < SILVER_BULLET_END):
                        continue

                ac = find_anchor_close(mnq_reset, bar_idx, direction)
                if ac is None:
                    continue
                pending_direction     = direction
                anchor_close          = ac
                pending_smt_sweep     = _smt_sweep
                pending_smt_miss      = _smt_miss
                divergence_bar_idx    = bar_idx
                _pending_div_bar_high = float(_mnq_highs[bar_idx])
                _pending_div_bar_low  = float(_mnq_lows[bar_idx])
                _pending_smt_defended = _smt_defended
                _pending_smt_type               = _smt_type
                _pending_fvg_zone               = _pending_fvg
                _pending_fvg_detected           = _raw_fvg_present
                _pending_displacement_bar_extreme = _displacement_bar_extreme
                state                           = "WAITING_FOR_ENTRY"
                _pending_div_score = divergence_score(
                    _smt_sweep, _smt_miss,
                    body_pts=abs(float(_mnq_closes[bar_idx]) - float(_mnq_opens[bar_idx])),
                    smt_type=_smt_type,
                    hypothesis_direction=_session_hyp_dir,
                    div_direction=direction,
                )
                if MIN_DIV_SCORE > 0 and _pending_div_score < MIN_DIV_SCORE:
                    state = "IDLE"
                    pending_direction = None
                    anchor_close = None
                    continue
                _pending_div_provisional     = (_smt_type == "displacement")
                _pending_discovery_bar_idx   = bar_idx
                _pending_discovery_price     = float(_mnq_closes[bar_idx])

        # End of session: force-close any position still open at session boundary.
        if state == "IN_TRADE" and position is not None:
            last_bar = mnq_session.iloc[-1]
            trade, day_pnl_delta = _build_trade_record(
                position, "session_close", last_bar, MNQ_PNL_PER_POINT
            )
            trade["matches_hypothesis"] = position.get("matches_hypothesis")
            for _f in ("hypothesis_direction", "pd_range_case", "pd_range_bias",
                       "week_zone", "day_zone", "trend_direction", "hypothesis_score",
                       "fvg_detected"):
                trade[_f] = position.get(_f)
            trades.append(trade)
            day_pnl += day_pnl_delta
            position = None
            entry_bar_count = 0

        # Reset all pending state at day boundary — signals don't carry across days.
        state = "IDLE"
        pending_direction = None
        anchor_close = None
        reentry_count         = 0
        prior_trade_bars_held = 0

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
            trade["matches_hypothesis"] = position.get("matches_hypothesis")
            for _f in ("hypothesis_direction", "pd_range_case", "pd_range_bias",
                       "week_zone", "day_zone", "trend_direction", "hypothesis_score",
                       "fvg_detected"):
                trade[_f] = position.get(_f)
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


def _write_trades_tsv(trades: list[dict]) -> None:
    """Write all test-fold trade records to trades.tsv (tab-separated). Overwrites each run."""
    import csv

    if not trades:
        return
    fieldnames = [
        "entry_date", "entry_time", "exit_time", "direction",
        "entry_price", "exit_price", "tdo", "stop_price", "contracts",
        "pnl", "exit_type", "divergence_bar", "entry_bar",
        "stop_bar_wick_pts", "reentry_sequence", "prior_trade_bars_held",
        "entry_bar_body_ratio", "smt_sweep_pts", "smt_miss_pts", "bars_since_divergence",
        "matches_hypothesis", "smt_type",
        "fvg_high", "fvg_low", "layer_b_entered", "layer_b_entry_price", "layer_b_contracts",
        "hypothesis_direction", "pd_range_case", "pd_range_bias",
        "week_zone", "day_zone", "trend_direction", "hypothesis_score", "fvg_detected",
        # Plan 4 columns
        "displacement_body_pts", "pessimistic_fills",
    ]
    with open("trades.tsv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t",
                           extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(trades)
    print(f"Trades written -> trades.tsv ({len(trades)} records)")


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
    _all_test_trades: list = []       # collect per-trade records for trades.tsv

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
        _all_test_trades.extend(_fold_test_stats.get("trade_records", []))

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
    _write_trades_tsv(_all_test_trades)