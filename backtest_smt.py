"""backtest_smt.py — SMT Divergence backtest harness. Frozen — do not modify. Run: uv run python backtest_smt.py"""
import datetime
import json
import math as _math
import os
import statistics
import sys
from pathlib import Path

import pandas as pd

from hypothesis_smt import compute_hypothesis_context
import strategy_smt
from strategy_smt import (
    _BarRow,
    init_bar_data, append_bar_data, load_futures_data, compute_tdo, find_anchor_close,
    is_confirmation_bar, detect_smt_divergence, _build_signal_from_bar,
    manage_position, screen_session,
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
    SMT_OPTIONAL, SMT_FILL_ENABLED, PARTIAL_EXIT_ENABLED, PARTIAL_EXIT_FRACTION, PARTIAL_STOP_BUFFER_PTS,
    DISPLACEMENT_STOP_MODE, MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT,
    FVG_LAYER_B_REQUIRES_HYPOTHESIS, STRUCTURAL_STOP_BUFFER_PTS,
    PESSIMISTIC_FILLS,
    LIMIT_ENTRY_BUFFER_PTS, LIMIT_EXPIRY_SECONDS, LIMIT_RATIO_THRESHOLD,
    CONFIRMATION_WINDOW_BARS,
    EXPANDED_REFERENCE_LEVELS, HTF_VISIBILITY_REQUIRED, HTF_PERIODS_MINUTES,
    HIDDEN_SMT_ENABLED, _compute_ref_levels, _check_smt_against_ref,
    ALWAYS_REQUIRE_CONFIRMATION,
    divergence_score, _effective_div_score,
    MIN_DIV_SCORE, REPLACE_THRESHOLD,
    DIV_SCORE_DECAY_FACTOR, DIV_SCORE_DECAY_INTERVAL, DIV_SCORE_DECAY_SECONDS,
    ADVERSE_MOVE_FULL_DECAY_PTS, ADVERSE_MOVE_MIN_DECAY,
    HYPOTHESIS_INVALIDATION_PTS,
    PARTIAL_EXIT_LEVEL_RATIO,
    TRAIL_AFTER_TP_PTS,
    MNQ_PNL_PER_POINT,
    _annotate_hypothesis, _build_draws_and_select,
    ScanState, SessionContext, process_scan_bar,
    detect_eqh_eql,
    EQH_ENABLED, EQH_SWING_BARS, EQH_TOLERANCE_PTS, EQH_MIN_TOUCHES, EQH_LOOKBACK_BARS,
    EVT_LIMIT_PLACED, EVT_LIMIT_MOVED, EVT_LIMIT_CANCELLED, EVT_LIMIT_FILLED, EVT_LIMIT_EXPIRED,
)

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
WALK_FORWARD_WINDOWS = int(os.environ.get("WALK_FORWARD_WINDOWS", "6"))
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

# Slippage applied to v2 market-close exits. 2 pts covers typical automated-trading
# latency (network + exchange queue) for MNQ in RTH. Will move to simulated-fill module.
V2_MARKET_CLOSE_SLIPPAGE_PTS: float = 2.0


def print_direction_breakdown(stats: dict, prefix: str = "") -> None:
    """Print per-direction trade count, win rate, avg PnL, and exit breakdown."""
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


def _size_contracts(entry_price: float, stop_price: float) -> "tuple[int, int]":
    """Return (contracts, total_contracts_target) based on risk params and two-layer split."""
    risk_per_contract = abs(entry_price - stop_price) * MNQ_PNL_PER_POINT
    contracts = (
        min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
        if risk_per_contract > 0 else 1
    )
    total_contracts_target = contracts
    if TWO_LAYER_POSITION:
        layer_a_contracts = max(1, int(contracts * LAYER_A_FRACTION))
        total_contracts_target = contracts
        contracts = layer_a_contracts
    return contracts, total_contracts_target


def _open_position(
    signal_dict: dict, day, contracts: int, total_contracts_target: int, bar_idx: int,
) -> dict:
    position = {
        **signal_dict, "entry_date": day, "contracts": contracts,
        "total_contracts_target": total_contracts_target,
        "layer_b_entered": False, "layer_b_entry_price": None,
        "layer_b_contracts": 0, "partial_done": False, "partial_price": None,
    }
    position["tp_breached"] = False
    position["entry_bar"]   = bar_idx
    # Human-mode additive fill slippage (direction-correct): long pays more, short receives less.
    # Read via module attribute so monkeypatching strategy_smt in tests takes effect.
    if strategy_smt.HUMAN_EXECUTION_MODE and strategy_smt.HUMAN_ENTRY_SLIPPAGE_PTS > 0:
        slip = strategy_smt.HUMAN_ENTRY_SLIPPAGE_PTS
        if position["direction"] == "long":
            position["entry_price"] = round(position["entry_price"] + slip, 4)
        else:
            position["entry_price"] = round(position["entry_price"] - slip, 4)
    return position


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
        "position_id":    position.get("position_id", -1),
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
        # Human-execution-mode diagnostic fields (Wave 5)
        "confidence":      position.get("confidence"),
        "signal_type":     position.get("signal_type"),
        "human_mode":      bool(strategy_smt.HUMAN_EXECUTION_MODE),
        "deception_exit":  "invalidation" in exit_result,
        # Solution F diagnostic fields
        "tp_name":               position.get("tp_name"),
        "secondary_target_name": position.get("secondary_target_name"),
        # Limit entry diagnostic fields
        "anchor_close_price": (
            round(position["anchor_close_price"], 4)
            if position.get("anchor_close_price") is not None else ""
        ),
        "limit_fill_bars":    position.get("limit_fill_bars", ""),
        "missed_move_pts":    "",
    }
    return trade, pnl


def _build_limit_expired_record(
    signal: dict,
    missed_move_pts: float,
    entry_date,
    expiry_ts,
    exit_type: str = "limit_expired",
) -> dict:
    """Build a trade-like record for a limit order that expired without filling."""
    return {
        "entry_date":        entry_date,
        "entry_time":        (
            signal["entry_time"].strftime("%H:%M")
            if hasattr(signal["entry_time"], "strftime")
            else str(signal["entry_time"])
        ),
        "exit_time":         (
            expiry_ts.strftime("%H:%M")
            if hasattr(expiry_ts, "strftime") else str(expiry_ts)
        ),
        "direction":         signal["direction"],
        "entry_price":       round(signal["entry_price"], 4),
        "exit_price":        "",
        "tdo":               round(signal.get("tdo", 0), 4),
        "stop_price":        round(signal["stop_price"], 4),
        "contracts":         0,
        "pnl":               0.0,
        "exit_type":         exit_type,
        "divergence_bar":    signal.get("divergence_bar", -1),
        "entry_bar":         signal.get("entry_bar", -1),
        "stop_bar_wick_pts": "",
        "reentry_sequence":  "",
        "prior_trade_bars_held": "",
        "entry_bar_body_ratio": round(signal.get("entry_bar_body_ratio", 0.0), 4),
        "smt_sweep_pts":     round(signal.get("smt_sweep_pts", 0.0), 4),
        "smt_miss_pts":      round(signal.get("smt_miss_pts", 0.0), 4),
        "bars_since_divergence": "",
        "matches_hypothesis": signal.get("matches_hypothesis"),
        "smt_type":          signal.get("smt_type", ""),
        "fvg_high":          signal.get("fvg_high", ""),
        "fvg_low":           signal.get("fvg_low", ""),
        "layer_b_entered":   False,
        "layer_b_entry_price": "",
        "layer_b_contracts": 0,
        "hypothesis_direction": signal.get("hypothesis_direction", ""),
        "pd_range_case":     signal.get("pd_range_case", ""),
        "pd_range_bias":     signal.get("pd_range_bias", ""),
        "week_zone":         signal.get("week_zone", ""),
        "day_zone":          signal.get("day_zone", ""),
        "trend_direction":   signal.get("trend_direction", ""),
        "hypothesis_score":  signal.get("hypothesis_score", ""),
        "fvg_detected":      signal.get("fvg_detected", ""),
        "displacement_body_pts": signal.get("displacement_body_pts", ""),
        "pessimistic_fills": PESSIMISTIC_FILLS,
        # Limit entry diagnostic fields
        "anchor_close_price": (
            round(signal["anchor_close_price"], 4)
            if signal.get("anchor_close_price") is not None else ""
        ),
        "limit_fill_bars":   "",
        "missed_move_pts":   round(missed_move_pts, 4),
    }


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


def _resolve_scan_result(result: "dict | None") -> "dict | None":
    """Unwrap lifecycle_batch and filter lifecycle-only events from scan results.

    Extracts the signal from a lifecycle_batch (if present), drops pure lifecycle
    events that should not open positions, and passes signals and expired events
    through unchanged.
    """
    if result is None:
        return None
    if result["type"] == "lifecycle_batch":
        signal_evts = [e for e in result["events"] if e["type"] == "signal"]
        return signal_evts[0] if signal_evts else None
    if result["type"] in (EVT_LIMIT_PLACED, EVT_LIMIT_MOVED, EVT_LIMIT_CANCELLED, EVT_LIMIT_FILLED):
        return None
    return result


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
    init_bar_data()
    start_dt = pd.Timestamp(start or BACKTEST_START).date()
    end_dt   = pd.Timestamp(end   or BACKTEST_END).date()

    trading_days = sorted({
        ts.date() for ts in mnq_df.index
        if start_dt <= ts.date() < end_dt
    })

    # State machine variables — persist across days for overnight positions.
    state = "IDLE"           # "IDLE" | "WAITING_FOR_ENTRY" | "IN_TRADE" | "REENTRY_ELIGIBLE"
    position: dict | None = None
    entry_bar_count = 0      # bars since entry, used for MAX_HOLD_BARS
    trades: list[dict] = []
    equity_curve: list[float] = [0.0]

    # Backtest-only counters — reset at session start via fresh ScanState or explicit reset.
    prior_trade_bars_held = 0    # how long the previous trade lasted
    _position_id          = 0    # monotonic counter; shared by partial + final records of same position

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
    _prev_appended_ts: "pd.Timestamp | None" = None

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

        # Append overnight / pre-session bars to strategy globals before any
        # session-init computation that may read them.
        _session_open_ts = mnq_session.index[0]
        _overnight_mnq = (
            mnq_df[mnq_df.index < _session_open_ts]
            if _prev_appended_ts is None
            else mnq_df[(mnq_df.index > _prev_appended_ts) & (mnq_df.index < _session_open_ts)]
        )
        if not _overnight_mnq.empty:
            _overnight_mes = mes_df[
                (mes_df.index >= _overnight_mnq.index[0]) & (mes_df.index <= _overnight_mnq.index[-1])
            ]
            append_bar_data(_overnight_mnq, _overnight_mes if not _overnight_mes.empty else None)

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

        # Gap 1: compute EQH/EQL from prior-day + overnight MNQ bars for secondary_target candidate pool.
        # Window = 2 days of history up to the session open — covers prior RTH + overnight reaction range.
        _day_eqh: list = []
        _day_eql: list = []
        if EQH_ENABLED:
            _session_start_ts = mnq_session.index[0]
            _eqh_window_start = _session_start_ts - pd.Timedelta(days=2)
            _eqh_bars = mnq_df[
                (mnq_df.index >= _eqh_window_start) & (mnq_df.index < _session_start_ts)
            ]
            if not _eqh_bars.empty:
                _day_eqh, _day_eql = detect_eqh_eql(
                    _eqh_bars, len(_eqh_bars),
                    lookback=EQH_LOOKBACK_BARS,
                    swing_bars=EQH_SWING_BARS,
                    tolerance=EQH_TOLERANCE_PTS,
                    min_touches=EQH_MIN_TOUCHES,
                )

        # Hypothesis direction + per-rule context for this session (deterministic, no LLM)
        _session_hyp_ctx = compute_hypothesis_context(mnq_df, _hist_mnq_df, day)
        _session_hyp_dir = _session_hyp_ctx["direction"] if _session_hyp_ctx else None

        # Fresh scan state each session — divergence signals are session-scoped.
        # ScanState carries all pending scan locals; SessionContext holds immutable session metadata.
        _scan_state = ScanState()
        if state != "IN_TRADE":
            _position_id = 0
            state = "IDLE"

        min_signal_ts = mnq_session.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)

        # Pre-compute reset-index views once per session to avoid repeated resets in the loop.
        mes_reset = mes_session.reset_index(drop=True)
        mnq_reset = mnq_session.reset_index(drop=True)

        # Detect bar resolution for limit expiry window conversion.
        # Uses only the first inter-bar gap — assumes a regular series with no late/missing open bar.
        if len(mnq_reset) >= 2:
            bar_seconds = (mnq_session.index[1] - mnq_session.index[0]).total_seconds()
        else:
            bar_seconds = 60.0

        # Pre-extract numpy arrays for fast per-bar access (avoids iterrows() Series overhead)
        _mnq_opens  = mnq_session["Open"].values
        _mnq_highs  = mnq_session["High"].values
        _mnq_lows   = mnq_session["Low"].values
        _mnq_closes = mnq_session["Close"].values
        _mnq_vols   = mnq_session["Volume"].values
        _mes_highs  = mes_session["High"].values
        _mes_lows   = mes_session["Low"].values
        _mes_closes = mes_session["Close"].values
        _mnq_idx    = mnq_session.index

        # Prev-day slices for EXPANDED_REFERENCE_LEVELS
        _prev_day = day - datetime.timedelta(days=1)
        _prev_day_date = _prev_day
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
            _week_start = day - datetime.timedelta(days=day.weekday())
            _week_mnq = mnq_df[(_mnq_dates >= _week_start) & (_mnq_dates < day)] \
                if _week_start < day else None
            _week_mes = mes_df[
                (mes_df.index.date >= _week_start) & (mes_df.index.date < day)
            ] if _week_start < day else None
            _bt_ref_lvls = _compute_ref_levels(
                _prev_day_mnq, _prev_day_mes, _prev_ses_mnq, _prev_ses_mes,
                _week_mnq, _week_mes,
            )

        # Immutable session context — passed to process_scan_bar each bar.
        # HTF state init now lives inside ScanState / process_scan_bar (removed from here).
        _session_ctx = SessionContext(
            day=day,
            tdo=day_tdo,
            midnight_open=_day_midnight_open,
            overnight=_day_overnight,
            pdh=_day_pdh,
            pdl=_day_pdl,
            hyp_ctx=_session_hyp_ctx,
            hyp_dir=_session_hyp_dir,
            bar_seconds=bar_seconds,
            ref_lvls=_bt_ref_lvls,
            eqh_levels=_day_eqh,   # Gap 1
            eql_levels=_day_eql,   # Gap 1
        )

        # Running session extremes — updated at start of each bar for bars [0..bar_idx-1]
        # Initialized to nan so bar 0 comparisons behave identically to empty-slice .max()/.min()
        _ses_mes_h  = _ses_mes_l  = float("nan")
        _ses_mnq_h  = _ses_mnq_l  = float("nan")
        _ses_mes_ch = _ses_mes_cl = float("nan")
        _ses_mnq_ch = _ses_mnq_cl = float("nan")
        # Running high/low for overnight sweep gate (replaces slice-based recomputation)
        _run_ses_high = -float("inf")  # symmetric with _run_ses_low; guard (_run_ses_high > _ep+1) filters it safely
        _run_ses_low  = float("inf")
        # Allocate smt_cache once; update values in-place each bar (avoids repeated dict creation)
        _smt_cache: dict = {
            "mes_h": float("nan"), "mes_l": float("nan"),
            "mnq_h": float("nan"), "mnq_l": float("nan"),
            "mes_ch": float("nan"), "mes_cl": float("nan"),
            "mnq_ch": float("nan"), "mnq_cl": float("nan"),
        }

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

            _smt_cache["mes_h"]  = _ses_mes_h;  _smt_cache["mes_l"]  = _ses_mes_l
            _smt_cache["mnq_h"]  = _ses_mnq_h;  _smt_cache["mnq_l"]  = _ses_mnq_l
            _smt_cache["mes_ch"] = _ses_mes_ch; _smt_cache["mes_cl"] = _ses_mes_cl
            _smt_cache["mnq_ch"] = _ses_mnq_ch; _smt_cache["mnq_cl"] = _ses_mnq_cl
            # HTF per-bar update is now handled inside process_scan_bar via state.htf_state.

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

                # Human-mode: "move_stop" is a non-exit event that surfaces a stop
                # mutation. Stop has already been applied to the position dict by
                # manage_position; treat the bar as a hold for state-machine purposes.
                if result == "move_stop":
                    position.pop("_pending_move_stop", None)
                    result = "hold"

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
                    partial_exit_price = position.get("partial_price", float(bar["Close"]))

                    # Stop-slide always runs regardless of trail mode — preserves near-breakeven
                    # floor on TDO-touch reversals.
                    _old_stop = position["stop_price"]
                    if position["direction"] == "long":
                        _lock_stop = partial_exit_price - PARTIAL_STOP_BUFFER_PTS
                        _new_stop  = max(_lock_stop, _old_stop)
                    else:
                        _lock_stop = partial_exit_price + PARTIAL_STOP_BUFFER_PTS
                        _new_stop  = min(_lock_stop, _old_stop)
                    position["stop_price"] = _new_stop

                    if TRAIL_AFTER_TP_PTS <= 0:
                        # Contract reduction only when not trailing
                        partial_contracts = min(
                            max(1, int(position["contracts"] * PARTIAL_EXIT_FRACTION)),
                            position["contracts"] - 1,
                        )
                        if partial_contracts < 1:
                            continue
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
                        _old_tp = position["take_profit"]
                        _sec = position.get("secondary_target")
                        _new_tp = _sec if _sec is not None else _old_tp
                        partial_trade["partial_adjustments"] = (
                            f"stop:{_old_stop:.2f}->{_new_stop:.2f};"
                            f"tp:{_old_tp:.2f}->{_new_tp:.2f}"
                        )
                        trades.append(partial_trade)
                        day_pnl += partial_pnl
                        position["contracts"] -= partial_contracts
                        position["take_profit"] = _new_tp
                        if _sec is not None:
                            position["secondary_target"] = None
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
                            _scan_state.scan_state = "REENTRY_ELIGIBLE"
                            _scan_state.anchor_close = float(bar["Close"])
                            _scan_state.conf_window_start = bar_idx + 1
                            _scan_state.pending_direction = position["direction"]
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
                                    _scan_state.scan_state = "IDLE"
                                    _scan_state.pending_direction = None
                                    _scan_state.anchor_close = None
                                else:
                                    _scan_state.scan_state = "REENTRY_ELIGIBLE"
                                    _scan_state.anchor_close = float(bar["Close"])
                                    _scan_state.conf_window_start = bar_idx + 1
                                    _scan_state.pending_direction = position["direction"]
                            else:
                                _scan_state.scan_state = "IDLE"
                                _scan_state.pending_direction = None
                                _scan_state.anchor_close = None
                    else:
                        _scan_state.scan_state = "IDLE"
                        _scan_state.pending_direction = None
                        _scan_state.anchor_close = None

                    _scan_state.prior_trade_bars_held = entry_bar_count
                    position = None
                    entry_bar_count = 0
                    state = _scan_state.scan_state  # sync for IN_TRADE check next bar

            else:  # not IN_TRADE — delegate scanning to process_scan_bar
                _scan_result = process_scan_bar(
                    _scan_state, _session_ctx, bar_idx, bar,
                    mnq_reset, mes_reset, _smt_cache,
                    _run_ses_high, _run_ses_low, ts, min_signal_ts,
                    _mnq_opens, _mnq_highs, _mnq_lows, _mnq_closes, _mnq_vols,
                    _mes_highs, _mes_lows, _mes_closes,
                )
                if _scan_result is None:
                    continue
                _scan_result = _resolve_scan_result(_scan_result)
                if _scan_result is None:
                    continue
                if _scan_result["type"] in ("expired", EVT_LIMIT_EXPIRED):
                    expired_record = _build_limit_expired_record(
                        _scan_result["signal"], _scan_result["limit_missed_move"], day, ts,
                    )
                    trades.append(expired_record)
                elif _scan_result["type"] == "signal":
                    signal = _scan_result
                    contracts, total_contracts_target = _size_contracts(
                        signal["entry_price"], signal["stop_price"]
                    )
                    position = _open_position(signal, day, contracts, total_contracts_target, bar_idx)
                    _position_id += 1
                    position["position_id"] = _position_id
                    state = "IN_TRADE"
                    entry_bar_count = 0

        # Bulk-append completed session bars to strategy globals.
        _ses_mes_slice = mes_df[
            (mes_df.index >= mnq_session.index[0]) & (mes_df.index <= mnq_session.index[-1])
        ]
        append_bar_data(
            mnq_session,
            _ses_mes_slice if not _ses_mes_slice.empty else None,
        )
        _prev_appended_ts = mnq_session.index[-1]

        # End of session: expire any pending limit order still waiting for fill.
        if _scan_state.scan_state == "WAITING_FOR_LIMIT_FILL" and _scan_state.pending_limit_signal is not None:
            last_ts = mnq_session.index[-1]
            expired_record = _build_limit_expired_record(
                _scan_state.pending_limit_signal, _scan_state.limit_missed_move, day, last_ts,
                exit_type="limit_expired_session_close",
            )
            trades.append(expired_record)
            _scan_state.pending_limit_signal = None
            _scan_state.scan_state = "IDLE"

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

        # Signals don't carry across days — ScanState is recreated at next session start.
        state = "IDLE"

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
        # Limit entry columns
        "anchor_close_price", "limit_fill_bars", "missed_move_pts",
        # Partial exit stop/tp adjustment notes
        "partial_adjustments",
        # Position linkage
        "position_id",
        # DOL target diagnostic (Solution F)
        "tp_name", "secondary_target_name",
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


# ---------------------------------------------------------------------------
# SMT v2 backtest harness
# ---------------------------------------------------------------------------

def _build_5m_bar_v2(session_bars: "pd.DataFrame", bar_ts: "pd.Timestamp") -> "dict | None":
    """Build a completed 5m bar dict from 1m session bars ending at bar_ts.

    The window spans [bar_ts - 4min, bar_ts] (5 bars total).
    Returns None if the window is empty.
    """
    start_5m = bar_ts - pd.Timedelta(minutes=4)
    window = session_bars.loc[start_5m:bar_ts]
    if window.empty:
        return None
    return {
        "time":      bar_ts.isoformat(),
        "open":      float(window.iloc[0]["Open"]),
        "high":      float(window["High"].max()),
        "low":       float(window["Low"].min()),
        "close":     float(window.iloc[-1]["Close"]),
        "body_high": max(float(window.iloc[0]["Open"]), float(window.iloc[-1]["Close"])),
        "body_low":  min(float(window.iloc[0]["Open"]), float(window.iloc[-1]["Close"])),
    }


def run_backtest_v2(start_date: str, end_date: str, *, write_events: bool = True) -> dict:
    """SMT v2 backtest: dispatches daily/hypothesis/trend/strategy per bar.

    Self-contained — does not use any globals from the existing run_backtest path.
    State JSON files are reset at the start of each day.

    Returns a dict with keys: trades, events, metrics.
    """
    import copy
    import json as _json

    import daily as _daily_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    import trend as _trend_mod
    from smt_state import (
        DEFAULT_DAILY, DEFAULT_GLOBAL, DEFAULT_HYPOTHESIS, DEFAULT_POSITION,
        save_daily, save_global, save_hypothesis, save_position,
    )
    from strategy_smt import load_futures_data

    futures = load_futures_data()
    mnq_all = futures["MNQ"]
    mes_all = futures["MES"]

    business_days = pd.bdate_range(start_date, end_date)

    all_trades: list[dict] = []
    all_events: list[dict] = []

    for bday in business_days:
        date = bday.date()

        # ------------------------------------------------------------------ #
        # Reset all four state files at the start of each day                 #
        # ------------------------------------------------------------------ #
        # Seed ATH from all historical bars so the hypothesis gate uses a real
        # cumulative high, not the default 0.0 which would be exceeded immediately.
        hist_for_ath = mnq_all[mnq_all.index.date < date]
        seeded_global = copy.deepcopy(DEFAULT_GLOBAL)
        if not hist_for_ath.empty:
            seeded_global["all_time_high"] = float(hist_for_ath["High"].max())
        save_global(seeded_global)
        save_daily(copy.deepcopy(DEFAULT_DAILY))
        save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))
        save_position(copy.deepcopy(DEFAULT_POSITION))

        # ------------------------------------------------------------------ #
        # Build per-day slices                                                 #
        # ------------------------------------------------------------------ #
        mnq_1m_today = mnq_all[mnq_all.index.date == date]
        mes_1m_today = mes_all[mes_all.index.date == date]

        if mnq_1m_today.empty:
            continue

        session_start_ts = pd.Timestamp(f"{date} 09:20:00", tz="America/New_York")
        hist_mnq_1m = mnq_all[mnq_all.index < session_start_ts]
        hist_mes_1m = mes_all[mes_all.index < session_start_ts]

        hist_hourly_mnq = (
            hist_mnq_1m.resample("1h", label="left").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
            if not hist_mnq_1m.empty
            else pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        )

        # ------------------------------------------------------------------ #
        # Run daily module once at 09:20 ET                                    #
        # ------------------------------------------------------------------ #
        now_daily = session_start_ts
        _daily_mod.run_daily(now_daily, mnq_1m_today[mnq_1m_today.index <= now_daily], hist_mnq_1m, hist_hourly_mnq)

        # Save levels snapshot for chart visualisation (after run_daily populates state)
        if write_events:
            from smt_state import load_daily as _ld, load_global as _lg
            _levels_snap = {
                "liquidities":  _ld().get("liquidities", []),
                "all_time_high": _lg().get("all_time_high"),
            }
            _lvl_path = Path(f"data/regression/{date}") / "levels.json"
            _lvl_path.parent.mkdir(parents=True, exist_ok=True)
            _lvl_path.write_text(_json.dumps(_levels_snap, indent=2), encoding="utf-8")

        # ------------------------------------------------------------------ #
        # Define 09:20–16:00 session window                                    #
        # ------------------------------------------------------------------ #
        session_end_ts = pd.Timestamp(f"{date} 16:00:00", tz="America/New_York")

        mnq_session_bars = mnq_1m_today[
            (mnq_1m_today.index >= session_start_ts) & (mnq_1m_today.index <= session_end_ts)
        ]
        mes_session_bars = mes_1m_today[
            (mes_1m_today.index >= session_start_ts) & (mes_1m_today.index <= session_end_ts)
        ]

        if mnq_session_bars.empty:
            continue

        day_events: list[dict] = []

        # ------------------------------------------------------------------ #
        # Per-bar loop                                                          #
        # ------------------------------------------------------------------ #
        for bar_ts in mnq_session_bars.index:
            now = bar_ts

            bar = mnq_session_bars.loc[bar_ts]
            _o = float(bar["Open"]); _c = float(bar["Close"])
            mnq_1m_bar = {
                "time":      bar_ts.isoformat(),
                "open":      _o,
                "high":      float(bar["High"]),
                "low":       float(bar["Low"]),
                "close":     _c,
                "body_high": max(_o, _c),
                "body_low":  min(_o, _c),
            }

            mnq_1m_recent = mnq_session_bars.loc[:bar_ts]

            is_5m_boundary = (bar_ts.minute % 5 == 0)

            # Trend runs first: validates existing hypothesis before a new one may form.
            trend_sig = _trend_mod.run_trend(now, mnq_1m_bar, mnq_1m_recent)
            if trend_sig is not None:
                if trend_sig.get("kind") == "market-close":
                    trend_sig["slippage"] = V2_MARKET_CLOSE_SLIPPAGE_PTS
                day_events.append(trend_sig)

            # Hypothesis runs on every 5m boundary, after trend has had a chance to clear state.
            if is_5m_boundary:
                hyp_divs = _hyp_mod.run_hypothesis(
                    now, mnq_session_bars.loc[:now], mes_session_bars.loc[:now],
                    hist_mnq_1m, hist_mes_1m,
                )
                if hyp_divs:
                    day_events.extend(hyp_divs)

            strat_sig = _strat_mod.run_strategy(now, mnq_1m_bar, mnq_1m_recent)
            if strat_sig is not None:
                if strat_sig.get("kind") in ("market-close", "market-entry"):
                    strat_sig["slippage"] = V2_MARKET_CLOSE_SLIPPAGE_PTS
                day_events.append(strat_sig)

        # ------------------------------------------------------------------ #
        # Emit end-of-session event if a position is still open               #
        # ------------------------------------------------------------------ #
        from smt_state import load_position as _load_pos
        _end_pos = _load_pos()
        if _end_pos.get("active"):
            _last_bar = mnq_session_bars.iloc[-1]
            _eod_ts   = mnq_session_bars.index[-1]
            day_events.append({
                "kind":      "end-of-session",
                "time":      _eod_ts.isoformat(),
                "price":     float(_last_bar["Close"]),
                "stop":      float(_end_pos["active"].get("stop", 0)),
                "direction": _end_pos["active"].get("direction", ""),
            })

        # ------------------------------------------------------------------ #
        # End-of-day trade pairing                                              #
        # ------------------------------------------------------------------ #
        day_trades: list[dict] = []
        entry_event: "dict | None" = None
        for evt in day_events:
            kind = evt.get("kind", "")
            if kind in ("limit-entry-filled", "market-entry"):
                entry_event = evt
            elif kind in ("market-close", "stopped-out", "end-of-session") and entry_event is not None:
                exit_reason = kind
                direction = entry_event.get("direction", "up")
                direction_sign = 1 if direction == "up" else -1
                entry_price = float(entry_event["price"])
                # Market entries carry slippage that goes against the trader (long pays more, short less).
                entry_slip = float(entry_event.get("slippage", 0.0))
                entry_price += direction_sign * entry_slip
                exit_price  = float(evt["price"])
                slip = float(evt.get("slippage", 0.0))
                exit_price -= direction_sign * slip
                contracts = 2  # default per spec
                pnl_points = (exit_price - entry_price) * direction_sign
                pnl_dollars = pnl_points * 2.0 * contracts
                day_trades.append({
                    "entry_time":  entry_event["time"],
                    "entry_price": entry_price,
                    "direction":   direction,
                    "contracts":   contracts,
                    "exit_time":   evt["time"],
                    "exit_price":  exit_price,
                    "exit_reason": exit_reason,
                    "pnl_points":  round(pnl_points, 4),
                    "pnl_dollars": round(pnl_dollars, 2),
                })
                entry_event = None

        all_events.extend(day_events)
        all_trades.extend(day_trades)

        # ------------------------------------------------------------------ #
        # Write per-day outputs                                                 #
        # ------------------------------------------------------------------ #
        if write_events:
            import os as _os
            out_dir = Path(f"data/regression/{date}")
            out_dir.mkdir(parents=True, exist_ok=True)

            # events.jsonl
            events_path = out_dir / "events.jsonl"
            with open(events_path, "w", encoding="utf-8") as _f:
                for evt in day_events:
                    _f.write(_json.dumps(evt, sort_keys=True) + "\n")

            # trades.tsv
            trades_path = out_dir / "trades.tsv"
            if day_trades:
                fieldnames = [
                    "entry_time", "entry_price", "direction", "contracts",
                    "exit_time", "exit_price", "exit_reason", "pnl_points", "pnl_dollars",
                ]
                import csv as _csv
                with open(trades_path, "w", newline="", encoding="utf-8") as _f:
                    w = _csv.DictWriter(_f, fieldnames=fieldnames, delimiter="\t",
                                        extrasaction="ignore")
                    w.writeheader()
                    w.writerows(day_trades)

    # ------------------------------------------------------------------ #
    # Aggregate metrics                                                     #
    # ------------------------------------------------------------------ #
    n_trades = len(all_trades)
    total_pnl = sum(t["pnl_dollars"] for t in all_trades)
    wins = sum(1 for t in all_trades if t["pnl_dollars"] > 0)
    win_rate = wins / n_trades if n_trades > 0 else 0.0

    return {
        "trades":  all_trades,
        "events":  all_events,
        "metrics": {
            "n_trades":  n_trades,
            "total_pnl": round(total_pnl, 2),
            "win_rate":  round(win_rate, 4),
        },
    }