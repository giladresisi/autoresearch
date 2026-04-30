# automation/main.py
# Live automation session process: realtime SMT divergence trading for MNQ1!/MES1!
# via PickMyTrade order routing.
# Mirrors signal_smt.py's state machine and IB realtime ingestion, but routes
# fills through PickMyTradeExecutor (async fills) instead of SimulatedFillExecutor.
"""
Usage: python -m automation.main

Requires IB Gateway (or TWS) running at IB_HOST:IB_PORT with API enabled and
PickMyTrade webhook credentials in environment (PMT_WEBHOOK_URL, PMT_API_KEY).
State machine: SCANNING → MANAGING, one trade per session.
"""
import os
from pathlib import Path
import json
import math as _math

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

from strategy_smt import (
    manage_position, compute_tdo, set_bar_data,
    compute_midnight_open, compute_overnight_range,
    compute_pdh_pdl,
    MIDNIGHT_OPEN_AS_TP, OVERNIGHT_SWEEP_REQUIRED, OVERNIGHT_RANGE_AS_TP,
    MIN_RR_FOR_TARGET, MIN_TARGET_PTS,
    MAX_REENTRY_COUNT,
    MIN_BARS_BEFORE_SIGNAL,
    detect_eqh_eql,
    EQH_ENABLED, EQH_SWING_BARS, EQH_TOLERANCE_PTS, EQH_MIN_TOUCHES, EQH_LOOKBACK_BARS,
)
# Human-mode flags read lazily via strategy_smt.<NAME> so monkeypatching
# strategy_smt attributes in tests affects this module without a re-import.
import strategy_smt
from hypothesis_smt import HypothesisManager
from data.ib_realtime import IbRealtimeSource
from execution.pickmytrade import PickMyTradeExecutor

# ── Connection constants ──────────────────────────────────────────────────────
IB_HOST      = "127.0.0.1"
IB_PORT      = 4002
# Use a different client ID to avoid conflicting with signal_smt.py (which uses 15)
IB_CLIENT_ID = int(os.environ.get("AUTOMATION_IB_CLIENT_ID", "20"))

# ── Contract identifiers (quarterly; update after rollover) ───────────────────
# MNQM6/MESM6 expire 2026-06-18; next: MNQU6=793356225, MESU6=793356217
MNQ_CONID = "770561201"
MES_CONID = "770561194"

# ── Session window ───────────────────────────────────────────────────────────
SESSION_START      = "09:00"   # ET
SIGNAL_SESSION_END = "13:30"   # ET  (named to avoid shadowing strategy_smt.SESSION_END)

# ── Trade configuration ───────────────────────────────────────────────────────
# 2-tick adverse fill; 1 tick = 0.25 MNQ points
ENTRY_SLIPPAGE_TICKS = 2
MAX_LOOKBACK_DAYS    = 30
GAP_FILL_MAX_DAYS    = 14  # covers long weekends and occasional missed days
MNQ_PNL_PER_POINT    = 2.0

# ── Reconnect settings ────────────────────────────────────────────────────────
MAX_RETRIES   = 10
RETRY_DELAY_S = 15

# ── Data paths ───────────────────────────────────────────────────────────────
BAR_DATA_DIR = Path("data")
POSITION_FILE = BAR_DATA_DIR / "live_position.json"
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "sessions"))

# ── Module-level state (set in main()) ───────────────────────────────────────
_ib_source: IbRealtimeSource | None = None
_executor: PickMyTradeExecutor | None = None

# Partial 1m MES bar accumulator — updated by the IbRealtimeSource via _on_bar callback
_mes_partial_1m: dict | None = None

_state: str = "SCANNING"          # "SCANNING" | "MANAGING"
_position: dict | None = None
_startup_ts: pd.Timestamp | None = None

# Guard against re-detecting a signal that fired before this session's first exit
_last_exit_ts: pd.Timestamp = pd.Timestamp("1970-01-01", tz="America/New_York")

# Per-session reentry tracking: reset when day changes or a new divergence level is detected
_current_session_date = None
_current_divergence_level: "float | None" = None
_divergence_reentry_count: int = 0

_hypothesis_manager: "HypothesisManager | None" = None
_hypothesis_generated: bool = False

# Historical daily OHLCV for PDH/PDL computation (loaded from parquet in main())
_hist_daily_df: pd.DataFrame = None

# Per-session PDH/PDL (updated once per trading day in _process_scanning)
_day_pdh: "float | None" = None
_day_pdl: "float | None" = None
_pdh_pdl_date = None  # tracks which date PDH/PDL was last computed

# Derived time objects (set from SESSION_START/SIGNAL_SESSION_END strings in main())
_session_start_time = None
_session_end_time   = None

# ── Stateful scanner state (Phase 5: one ScanState per trading day) ───────────
_scan_state: "strategy_smt.ScanState | None" = None
_session_ctx: "strategy_smt.SessionContext | None" = None
_session_init_date = None   # date of last session init; triggers re-init on day change
_session_mnq_rows: list = []   # accumulated MNQ bar dicts for the current session
_session_mes_rows: list = []   # accumulated MES bar dicts for the current session
# Running session extremes used for smt_cache and run_ses_high/low
_session_smt_cache: dict = {
    "mes_h": float("nan"), "mes_l": float("nan"),
    "mnq_h": float("nan"), "mnq_l": float("nan"),
    "mes_ch": float("nan"), "mes_cl": float("nan"),
    "mnq_ch": float("nan"), "mnq_cl": float("nan"),
}
_session_run_ses_high: float = -float("inf")
_session_run_ses_low: float  =  float("inf")

# Human-mode MOVE_STOP rate limiter — tracks the bar index of the last emission
# so we can suppress MOVE_STOP events that arrive within MOVE_STOP_MIN_GAP_BARS.
_last_move_stop_bar_idx: int = -10**9
_move_stop_bar_counter: int  = 0

# ── v2 pipeline env gate (set in main()) ─────────────────────────────────────
_smtv2_pipeline: str = "v1"
_smtv2_dispatcher: "SmtV2Dispatcher | None" = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _on_bar(bar, mes_partial) -> None:
    """Callback fired by IbRealtimeSource on each second boundary.

    Receives the current MNQ partial-1m bar (as a _BarRow) and the MES partial accumulator
    dict. Updates the module-level MES partial so _process_scanning can read it.
    """
    global _mes_partial_1m
    _mes_partial_1m = mes_partial
    _process(bar)


def _bar_timestamp(bar) -> pd.Timestamp:
    # IB bars expose .date; strategy_smt._BarRow exposes .name
    ts = pd.Timestamp(getattr(bar, "date", None) or bar.name)
    if ts.tz is None:
        return ts.tz_localize("America/New_York")
    return ts.tz_convert("America/New_York")


def _compute_pnl(position: dict, exit_price: float) -> float:
    """P&L in dollars for one MNQ contract given exit price."""
    sign = 1 if position["direction"] == "long" else -1
    return sign * (exit_price - position["assumed_entry"]) * position["contracts"] * MNQ_PNL_PER_POINT


def _format_signal_line(ts: pd.Timestamp, signal: dict, assumed_entry: float) -> str:
    """Human-readable signal log line."""
    dist = abs(signal["entry_price"] - signal["take_profit"])
    stop_dist = abs(signal["entry_price"] - signal["stop_price"])
    rr = dist / stop_dist if stop_dist > 0 else 0.0
    slip_label = f"+{ENTRY_SLIPPAGE_TICKS}t slip" if signal["direction"] == "long" else f"-{ENTRY_SLIPPAGE_TICKS}t slip"
    entry_time = pd.Timestamp(signal["entry_time"])
    signal_type = signal.get("signal_type", "UNKNOWN")
    return (
        f"[{ts.strftime('%H:%M:%S')}] SIGNAL    {signal['direction']:<5} | "
        f"entry_time {entry_time.strftime('%H:%M:%S')} | "
        f"entry ~{assumed_entry:.2f} ({slip_label}) | "
        f"stop {signal['stop_price']:.2f} | "
        f"TP {signal['take_profit']:.2f} | "
        f"RR ~{rr:.1f}x | "
        f"type {signal_type}"
    )


def _format_exit_line(
    ts: pd.Timestamp, exit_type: str, exit_price: float, pnl: float, contracts: int,
    entry_time_str: str,
) -> str:
    """Human-readable exit log line."""
    label = exit_type.replace("exit_", "").replace("_session_end", " (session)")
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    entry_ts = pd.Timestamp(entry_time_str)
    if entry_ts.tz is None:
        entry_ts = entry_ts.tz_localize("America/New_York")
    dur_secs = int((ts - entry_ts).total_seconds())
    dur_str = f"{dur_secs // 60}m {dur_secs % 60}s"
    return (
        f"[{ts.strftime('%H:%M:%S')}] EXIT      {label:<6} | "
        f"filled {exit_price:.2f} | "
        f"P&L {pnl_str} | "
        f"duration {dur_str} | "
        f"{contracts} MNQ1! contract{'s' if contracts != 1 else ''}"
    )


def _format_stop_moved_line(ts: pd.Timestamp, reason: str, new_stop: float, old_stop: float) -> str:
    """Human-readable stop-mutation log line."""
    direction = "->" if new_stop != old_stop else "="
    return (
        f"[{ts.strftime('%H:%M:%S')}] STOP_MOVE {reason:<10} | "
        f"stop {old_stop:.2f} {direction} {new_stop:.2f}"
    )


def _format_limit_placed_line(ts: pd.Timestamp, signal: dict) -> str:
    """Human-readable LIMIT_PLACED log line."""
    dist = abs(signal["entry_price"] - signal["take_profit"])
    stop_dist = abs(signal["entry_price"] - signal["stop_price"])
    rr = dist / stop_dist if stop_dist > 0 else 0.0
    return (
        f"[{ts.strftime('%H:%M:%S')}] LIMIT_PLACED  {signal['direction']:<5} | "
        f"entry {signal['entry_price']:.2f} | "
        f"stop {signal['stop_price']:.2f} | "
        f"TP {signal['take_profit']:.2f} | "
        f"RR ~{rr:.1f}x"
    )


def _format_limit_moved_line(ts: pd.Timestamp, old: dict, new: dict) -> str:
    """Human-readable LIMIT_MOVED log line."""
    return (
        f"[{ts.strftime('%H:%M:%S')}] LIMIT_MOVED   {new['direction']:<5} | "
        f"entry {old['entry_price']:.2f} -> {new['entry_price']:.2f} | "
        f"stop {old['stop_price']:.2f} -> {new['stop_price']:.2f} | "
        f"TP {old['take_profit']:.2f} -> {new['take_profit']:.2f}"
    )


def _format_limit_cancelled_line(ts: pd.Timestamp, signal: dict, reason: str) -> str:
    """Human-readable LIMIT_CANCELLED log line."""
    return (
        f"[{ts.strftime('%H:%M:%S')}] LIMIT_CANCELLED {signal['direction']:<5} | "
        f"entry {signal['entry_price']:.2f} | "
        f"reason {reason}"
    )


def _format_limit_expired_line(ts: pd.Timestamp, signal: dict, missed_move: float) -> str:
    """Human-readable LIMIT_EXPIRED log line."""
    return (
        f"[{ts.strftime('%H:%M:%S')}] LIMIT_EXPIRED  {signal['direction']:<5} | "
        f"entry {signal['entry_price']:.2f} | "
        f"missed {missed_move:.1f} pts"
    )


def _format_limit_filled_line(ts: pd.Timestamp, evt: dict) -> str:
    """Human-readable LIMIT_FILLED log line."""
    return (
        f"[{ts.strftime('%H:%M:%S')}] LIMIT_FILLED  {evt['direction']:<5} | "
        f"filled {evt['filled_price']:.2f} | "
        f"orig {evt['original_limit_price']:.2f} | "
        f"queue_s {evt['time_in_queue_secs']:.0f}"
    )


def _load_hist_mnq() -> pd.DataFrame:
    """Load the Databento 5m historical parquet for hypothesis rule engine."""
    hist_path = Path("data/MNQ.parquet")
    if hist_path.exists():
        return pd.read_parquet(hist_path)
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


# ── State machine ─────────────────────────────────────────────────────────────

def _process(bar) -> None:
    """Route each 1s bar to the appropriate state handler."""
    bar_ts   = _bar_timestamp(bar)
    bar_time = bar_ts.time()

    if _state == "SCANNING":
        _process_scanning(bar, bar_ts, bar_time)
    else:
        _process_managing(bar, bar_ts, bar_time)


def _dispatch_event(bar_ts: pd.Timestamp, evt: dict) -> None:
    """Print human-readable log line + JSON payload for a single lifecycle event."""
    evt_type = evt.get("type")
    if evt_type == "limit_placed":
        print(_format_limit_placed_line(bar_ts, evt["signal"]), flush=True)
        print(json.dumps({
            "signal_type":          "LIMIT_PLACED",
            "direction":            evt["signal"]["direction"],
            "entry_price":          round(float(evt["signal"]["entry_price"]), 4),
            "stop_price":           round(float(evt["signal"]["stop_price"]), 4),
            "take_profit":          round(float(evt["signal"]["take_profit"]), 4),
            "tp_name":              evt["signal"].get("tp_name"),
            "confidence":           evt["signal"].get("confidence"),
            "divergence_bar_time":  evt["signal"].get("divergence_bar_time"),
        }), flush=True)
    elif evt_type == "limit_moved":
        print(_format_limit_moved_line(bar_ts, evt["old_signal"], evt["new_signal"]), flush=True)
        print(json.dumps({
            "signal_type":          "MOVE_LIMIT",
            "direction":            evt["new_signal"]["direction"],
            "old_entry_price":      round(float(evt["old_signal"]["entry_price"]), 4),
            "new_entry_price":      round(float(evt["new_signal"]["entry_price"]), 4),
            "old_stop_price":       round(float(evt["old_signal"]["stop_price"]), 4),
            "new_stop_price":       round(float(evt["new_signal"]["stop_price"]), 4),
            "old_take_profit":      round(float(evt["old_signal"]["take_profit"]), 4),
            "new_take_profit":      round(float(evt["new_signal"]["take_profit"]), 4),
            "reason":               "anchor_shift",
        }), flush=True)
    elif evt_type == "limit_cancelled":
        print(_format_limit_cancelled_line(bar_ts, evt["signal"], evt.get("reason", "unknown")), flush=True)
        print(json.dumps({
            "signal_type":  "CANCEL_LIMIT",
            "direction":    evt["signal"]["direction"],
            "entry_price":  round(float(evt["signal"]["entry_price"]), 4),
            "reason":       evt.get("reason", "unknown"),
        }), flush=True)
    elif evt_type in ("limit_expired", "expired"):
        sig = evt.get("signal", {})
        missed = float(evt.get("limit_missed_move", 0.0))
        print(_format_limit_expired_line(bar_ts, sig, missed), flush=True)
        print(json.dumps({
            "signal_type":      "LIMIT_EXPIRED",
            "direction":        sig.get("direction"),
            "entry_price":      round(float(sig.get("entry_price", 0)), 4),
            "missed_move_pts":  round(missed, 4),
        }), flush=True)
    elif evt_type == "limit_filled":
        print(_format_limit_filled_line(bar_ts, evt), flush=True)
        print(json.dumps({
            "signal_type":          "LIMIT_FILLED",
            "direction":            evt["direction"],
            "filled_price":         round(float(evt["filled_price"]), 4),
            "original_limit_price": round(float(evt["original_limit_price"]), 4),
            "time_in_queue_secs":   round(float(evt["time_in_queue_secs"]), 1),
        }), flush=True)
    elif evt_type == "lifecycle_batch":
        for sub in evt.get("events", []):
            _dispatch_event(bar_ts, sub)
    elif evt_type == "signal":
        pass  # signal events handled by existing downstream code after _dispatch_event returns
    else:
        # Unknown event type: log warning without crashing
        print(f"[{bar_ts.strftime('%H:%M:%S')}] WARNING unknown event type: {evt_type}", flush=True)


def _process_scanning(bar, bar_ts: pd.Timestamp, bar_time) -> None:
    """SCANNING state: stateful per-bar signal detection via process_scan_bar."""
    global _state, _position, _last_exit_ts
    global _current_session_date, _current_divergence_level, _divergence_reentry_count
    global _day_pdh, _day_pdl, _pdh_pdl_date
    global _scan_state, _session_ctx, _session_init_date
    global _session_mnq_rows, _session_mes_rows
    global _session_smt_cache, _session_run_ses_high, _session_run_ses_low

    # 1. Session gate
    if bar_time < _session_start_time or bar_time > _session_end_time:
        return

    # 1a. Hypothesis gate
    if _hypothesis_manager is not None and not _hypothesis_generated:
        return

    # 2. Alignment gate: MES partial bar must exist and align with this 1s bar's minute
    if _mes_partial_1m is None:
        return
    if _mes_partial_1m["minute_ts"] != bar_ts.floor("min"):
        return

    today = bar_ts.date()

    # IB realtime data frames (read lazily so tests can monkeypatch _ib_source)
    _mnq_1m_df = _ib_source.mnq_1m_df if _ib_source is not None else None
    _mes_1m_df = _ib_source.mes_1m_df if _ib_source is not None else None

    # 3. Session metadata (computed once; fast on subsequent bars due to caching)
    tdo = compute_tdo(_mnq_1m_df, today)
    if tdo is None:
        return
    midnight_open_price = compute_midnight_open(_mnq_1m_df, today) if MIDNIGHT_OPEN_AS_TP else None
    overnight_range = (
        compute_overnight_range(_mnq_1m_df, today)
        if (OVERNIGHT_SWEEP_REQUIRED or OVERNIGHT_RANGE_AS_TP)
        else None
    )
    if today != _pdh_pdl_date and _hist_daily_df is not None:
        _day_pdh, _day_pdl = compute_pdh_pdl(_hist_daily_df, today)
        _pdh_pdl_date = today

    # 4. Session state init: reset ScanState and SessionContext on new trading day.
    # Pre-loads historical 1m session bars to compute running extremes (no state replay;
    # any pre-startup signals are filtered by _startup_ts guard downstream).
    _session_just_inited = False
    if today != _session_init_date:
        _session_init_date = today
        _scan_state = strategy_smt.ScanState()
        _hyp_dir = _hypothesis_manager.direction_bias if _hypothesis_manager else None
        _hyp_ctx = (
            _hypothesis_manager.context_dict
            if _hypothesis_manager and hasattr(_hypothesis_manager, "context_dict")
            else None
        )
        # Gap 1: compute EQH/EQL from the loaded 1m history (pre-session + overnight slice).
        _eqh_levels: list = []
        _eql_levels: list = []
        if EQH_ENABLED and _mnq_1m_df is not None and not _mnq_1m_df.empty:
            _session_start_dt = pd.Timestamp(f"{today} {SESSION_START}", tz="America/New_York")
            _eqh_window_start = _session_start_dt - pd.Timedelta(days=2)
            _eqh_bars = _mnq_1m_df[
                (_mnq_1m_df.index >= _eqh_window_start) & (_mnq_1m_df.index < _session_start_dt)
            ]
            if not _eqh_bars.empty:
                _eqh_levels, _eql_levels = detect_eqh_eql(
                    _eqh_bars, len(_eqh_bars),
                    lookback=EQH_LOOKBACK_BARS,
                    swing_bars=EQH_SWING_BARS,
                    tolerance=EQH_TOLERANCE_PTS,
                    min_touches=EQH_MIN_TOUCHES,
                )

        _session_ctx = strategy_smt.SessionContext(
            day=today, tdo=tdo,
            midnight_open=midnight_open_price,
            overnight=overnight_range or {},
            pdh=_day_pdh, pdl=_day_pdl,
            hyp_ctx=_hyp_ctx, hyp_dir=_hyp_dir,
            bar_seconds=1.0,
            eqh_levels=_eqh_levels,
            eql_levels=_eql_levels,
        )
        _session_mnq_rows = []
        _session_mes_rows = []
        _session_smt_cache = {
            "mes_h": float("nan"), "mes_l": float("nan"),
            "mnq_h": float("nan"), "mnq_l": float("nan"),
            "mes_ch": float("nan"), "mes_cl": float("nan"),
            "mnq_ch": float("nan"), "mnq_cl": float("nan"),
        }
        _session_run_ses_high = -float("inf")
        _session_run_ses_low  =  float("inf")
        # Pre-load today's completed 1m session bars to populate running extremes.
        if _mnq_1m_df is not None and not _mnq_1m_df.empty:
            _hist_mask = (
                (_mnq_1m_df.index.date == today)
                & (_mnq_1m_df.index.time >= _session_start_time)
            )
            for _ts_h, _row_h in _mnq_1m_df[_hist_mask].iterrows():
                _session_mnq_rows.append({
                    "Open": float(_row_h["Open"]), "High": float(_row_h["High"]),
                    "Low": float(_row_h["Low"]), "Close": float(_row_h["Close"]),
                    "Volume": float(_row_h.get("Volume", 0.0)),
                })
                _v = float(_row_h["High"])
                _session_smt_cache["mnq_h"] = (
                    _v if _math.isnan(_session_smt_cache["mnq_h"])
                    else max(_session_smt_cache["mnq_h"], _v)
                )
                _session_run_ses_high = max(_session_run_ses_high, _v)
                _v = float(_row_h["Low"])
                _session_smt_cache["mnq_l"] = (
                    _v if _math.isnan(_session_smt_cache["mnq_l"])
                    else min(_session_smt_cache["mnq_l"], _v)
                )
                _session_run_ses_low = min(_session_run_ses_low, _v)
                _v = float(_row_h["Close"])
                _session_smt_cache["mnq_ch"] = (
                    _v if _math.isnan(_session_smt_cache["mnq_ch"])
                    else max(_session_smt_cache["mnq_ch"], _v)
                )
                _session_smt_cache["mnq_cl"] = (
                    _v if _math.isnan(_session_smt_cache["mnq_cl"])
                    else min(_session_smt_cache["mnq_cl"], _v)
                )
        if _mes_1m_df is not None and not _mes_1m_df.empty:
            _mes_hist_mask = (
                (_mes_1m_df.index.date == today)
                & (_mes_1m_df.index.time >= _session_start_time)
            )
            for _ts_h, _row_h in _mes_1m_df[_mes_hist_mask].iterrows():
                _session_mes_rows.append({
                    "Open": float(_row_h["Open"]), "High": float(_row_h["High"]),
                    "Low": float(_row_h["Low"]), "Close": float(_row_h["Close"]),
                    "Volume": float(_row_h.get("Volume", 0.0)),
                })
                _v = float(_row_h["High"])
                _session_smt_cache["mes_h"] = (
                    _v if _math.isnan(_session_smt_cache["mes_h"])
                    else max(_session_smt_cache["mes_h"], _v)
                )
                _v = float(_row_h["Low"])
                _session_smt_cache["mes_l"] = (
                    _v if _math.isnan(_session_smt_cache["mes_l"])
                    else min(_session_smt_cache["mes_l"], _v)
                )
                _v = float(_row_h["Close"])
                _session_smt_cache["mes_ch"] = (
                    _v if _math.isnan(_session_smt_cache["mes_ch"])
                    else max(_session_smt_cache["mes_ch"], _v)
                )
                _session_smt_cache["mes_cl"] = (
                    _v if _math.isnan(_session_smt_cache["mes_cl"])
                    else min(_session_smt_cache["mes_cl"], _v)
                )
        _session_just_inited = True

    # 5. Update running extremes from previous bar (before appending current bar).
    # Skipped on session init: the init block already folded all historical bars into the
    # cache; re-reading the last row here would double-count its extremes.
    if not _session_just_inited and _session_mnq_rows:
        _prev = _session_mnq_rows[-1]
        _v = float(_prev["High"])
        _session_smt_cache["mnq_h"] = (
            _v if _math.isnan(_session_smt_cache["mnq_h"])
            else max(_session_smt_cache["mnq_h"], _v)
        )
        _session_run_ses_high = max(_session_run_ses_high, _v)
        _v = float(_prev["Low"])
        _session_smt_cache["mnq_l"] = (
            _v if _math.isnan(_session_smt_cache["mnq_l"])
            else min(_session_smt_cache["mnq_l"], _v)
        )
        _session_run_ses_low = min(_session_run_ses_low, _v)
        _v = float(_prev["Close"])
        _session_smt_cache["mnq_ch"] = (
            _v if _math.isnan(_session_smt_cache["mnq_ch"])
            else max(_session_smt_cache["mnq_ch"], _v)
        )
        _session_smt_cache["mnq_cl"] = (
            _v if _math.isnan(_session_smt_cache["mnq_cl"])
            else min(_session_smt_cache["mnq_cl"], _v)
        )
    if not _session_just_inited and _session_mes_rows:
        _prev_m = _session_mes_rows[-1]
        _v = float(_prev_m["High"])
        _session_smt_cache["mes_h"] = (
            _v if _math.isnan(_session_smt_cache["mes_h"])
            else max(_session_smt_cache["mes_h"], _v)
        )
        _v = float(_prev_m["Low"])
        _session_smt_cache["mes_l"] = (
            _v if _math.isnan(_session_smt_cache["mes_l"])
            else min(_session_smt_cache["mes_l"], _v)
        )
        _v = float(_prev_m["Close"])
        _session_smt_cache["mes_ch"] = (
            _v if _math.isnan(_session_smt_cache["mes_ch"])
            else max(_session_smt_cache["mes_ch"], _v)
        )
        _session_smt_cache["mes_cl"] = (
            _v if _math.isnan(_session_smt_cache["mes_cl"])
            else min(_session_smt_cache["mes_cl"], _v)
        )

    # 6. Append current MNQ and MES bars to session rows
    _session_mnq_rows.append({
        "Open": float(bar.Open), "High": float(bar.High),
        "Low": float(bar.Low), "Close": float(bar.Close), "Volume": float(bar.Volume),
    })
    _session_mes_rows.append({
        "Open": float(_mes_partial_1m["open"]), "High": float(_mes_partial_1m["high"]),
        "Low": float(_mes_partial_1m["low"]), "Close": float(_mes_partial_1m["close"]),
        "Volume": float(_mes_partial_1m["volume"]),
    })
    bar_idx = len(_session_mnq_rows) - 1

    # 7. Build DataFrames from accumulated rows
    mnq_reset = pd.DataFrame(_session_mnq_rows)
    mes_reset = pd.DataFrame(_session_mes_rows)
    _min_n = min(len(mnq_reset), len(mes_reset))
    mnq_reset = mnq_reset.iloc[:_min_n].reset_index(drop=True)
    mes_reset  = mes_reset.iloc[:_min_n].reset_index(drop=True)

    # 8. Extract numpy arrays for process_scan_bar
    _mnq_o = mnq_reset["Open"].values
    _mnq_h = mnq_reset["High"].values
    _mnq_l = mnq_reset["Low"].values
    _mnq_c = mnq_reset["Close"].values
    _mnq_v = mnq_reset["Volume"].values
    _mes_h = mes_reset["High"].values
    _mes_l = mes_reset["Low"].values
    _mes_c = mes_reset["Close"].values

    _bar_row = strategy_smt._BarRow(
        float(bar.Open), float(bar.High), float(bar.Low), float(bar.Close),
        float(bar.Volume), bar_ts,
    )
    _min_signal_ts = bar_ts.normalize().replace(
        hour=_session_start_time.hour, minute=_session_start_time.minute, second=0
    ) + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)

    # 9. Call stateful scanner — O(1) per bar, no rescan from bar 0
    result = strategy_smt.process_scan_bar(
        _scan_state, _session_ctx, bar_idx, _bar_row,
        mnq_reset, mes_reset, _session_smt_cache,
        _session_run_ses_high, _session_run_ses_low,
        bar_ts, _min_signal_ts,
        _mnq_o, _mnq_h, _mnq_l, _mnq_c, _mnq_v,
        _mes_h, _mes_l, _mes_c,
    )
    if result is None:
        return
    evt_type = result["type"]
    if evt_type == "lifecycle_batch":
        for _sub in result["events"]:
            _dispatch_event(bar_ts, _sub)
        # Extract signal from batch for downstream handling
        _signal_evts = [e for e in result["events"] if e["type"] == "signal"]
        if not _signal_evts:
            return
        result = _signal_evts[0]
        evt_type = "signal"
    if evt_type != "signal":
        _dispatch_event(bar_ts, result)
        return
    signal = result

    # 10. Live-only guards (startup, re-detection, per-divergence reentry)
    if _startup_ts is not None and signal["entry_time"] <= _startup_ts:
        return
    if signal["entry_time"] <= _last_exit_ts:
        return

    if today != _current_session_date:
        _current_session_date = today
        _current_divergence_level = None
        _divergence_reentry_count = 0

    defended = signal.get("smt_defended_level")
    if defended != _current_divergence_level:
        _current_divergence_level = defended
        _divergence_reentry_count = 0
    if MAX_REENTRY_COUNT < 999 and _divergence_reentry_count >= MAX_REENTRY_COUNT:
        return

    # 10a. Human-mode confidence filter + ENTRY_LIMIT/ENTRY_MARKET classification.
    # HUMAN_EXECUTION_MODE gate removed — main() always sets it True; keep guard for
    # safety in case this code path is reached from a non-main() caller.
    if strategy_smt.HUMAN_EXECUTION_MODE:
        conf = signal.get("confidence")
        if conf is not None and conf < strategy_smt.MIN_CONFIDENCE_THRESHOLD:
            # Sub-threshold: suppressed (internal log only); no emission, no position.
            print(
                f"[{bar_ts.strftime('%H:%M:%S')}] SUPPRESSED {signal['direction']:<5} | "
                f"confidence {conf:.2f} < {strategy_smt.MIN_CONFIDENCE_THRESHOLD:.2f}",
                flush=True,
            )
            return
        # Classify deterministically: ENTRY_LIMIT if the algo went through a limit stage
        # (limit_fill_bars is set by WAITING_FOR_LIMIT_FILL and same-bar limit paths);
        # ENTRY_MARKET otherwise.
        if signal.get("limit_fill_bars") is not None:
            signal["signal_type"] = "ENTRY_LIMIT"
        else:
            signal["signal_type"] = "ENTRY_MARKET"

    # 11. Open position with executor-supplied fill price
    _bar_row_for_fill = strategy_smt._BarRow(
        float(bar.Open), float(bar.High), float(bar.Low), float(bar.Close),
        float(bar.Volume), bar_ts,
    )
    _entry_fill = _executor.place_entry(signal, _bar_row_for_fill)
    # PickMyTradeExecutor returns None (async fill); fall back to signal price for display
    assumed_entry = _entry_fill.fill_price if _entry_fill else float(signal["entry_price"])
    _position = {
        **signal,
        "assumed_entry": assumed_entry,
        "contracts": 1,
        "instrument": "MNQ1!",
        "entry_time": str(signal["entry_time"]),
        "tp_breached": False,
    }
    POSITION_FILE.write_text(json.dumps(_position, indent=2))
    print(_format_signal_line(bar_ts, signal, assumed_entry), flush=True)
    if strategy_smt.HUMAN_EXECUTION_MODE:
        # Emit the typed human-trader payload alongside the legacy log line.
        print(
            json.dumps({
                "signal_type":    signal["signal_type"],
                "direction":      signal["direction"],
                "entry_price":    round(float(signal["entry_price"]), 4),
                "initial_stop":   round(float(signal["stop_price"]), 4),
                "tp":             round(float(signal["take_profit"]), 4),
                "confidence":     signal.get("confidence"),
                "valid_for_bars": None,
                "reason":         signal.get("smt_type", "wick"),
            }),
            flush=True,
        )
    _divergence_reentry_count += 1
    _state = "MANAGING"


def _process_managing(bar, bar_ts: pd.Timestamp, bar_time) -> None:
    """MANAGING state: check exit conditions on each 1s bar.

    Exits on TP, stop, or session end; prints exit line and resets to SCANNING.
    """
    global _state, _position, _last_exit_ts
    global _last_move_stop_bar_idx, _move_stop_bar_counter

    bar_row = strategy_smt._BarRow(
        float(bar.Open), float(bar.High), float(bar.Low), float(bar.Close),
        float(bar.Volume), bar_ts,
    )

    old_stop        = _position.get("stop_price")
    old_tp_breached = _position.get("tp_breached", False)
    old_breakeven   = _position.get("breakeven_active", False)

    result = manage_position(_position, bar_row)

    # Human-mode: manage_position may surface a MOVE_STOP event instead of silently
    # mutating the stop. Emit the structured payload (subject to rate limit) and
    # carry on as if the bar returned "hold".
    if result == "move_stop":
        _move_stop_bar_counter += 1
        new_stop = _position.get("_pending_move_stop", _position.get("stop_price"))
        gap = _move_stop_bar_counter - _last_move_stop_bar_idx
        if gap >= strategy_smt.MOVE_STOP_MIN_GAP_BARS:
            reason = "breakeven" if _position.get("breakeven_active") else "trail_update"
            urgency = "high" if reason == "breakeven" else "low"
            print(
                json.dumps({
                    "signal_type": "MOVE_STOP",
                    "new_stop":    round(float(new_stop), 4),
                    "reason":      reason,
                    "urgency":     urgency,
                }),
                flush=True,
            )
            _last_move_stop_bar_idx = _move_stop_bar_counter
        # Clear the pending marker so a subsequent hold doesn't re-trigger emission.
        _position.pop("_pending_move_stop", None)
        result = "hold"

    if result == "hold":
        if bar_time >= _session_end_time:
            # Force close at session end — no TP/stop triggered
            result = "exit_session_end"
            exit_price = float(bar.Close)
        else:
            # Log any stop mutations before persisting to disk.
            new_stop = _position.get("stop_price")
            if _position.get("breakeven_active") and not old_breakeven:
                print(_format_stop_moved_line(bar_ts, "breakeven", new_stop, old_stop), flush=True)
            elif _position.get("tp_breached") and not old_tp_breached:
                print(_format_stop_moved_line(bar_ts, "trail_start", new_stop, old_stop), flush=True)
            elif _position.get("tp_breached") and new_stop != old_stop:
                print(_format_stop_moved_line(bar_ts, "trail", new_stop, old_stop), flush=True)
            POSITION_FILE.write_text(json.dumps(_position, indent=2))
            return

    if result == "partial_exit":
        _exit_fill = _executor.place_exit(_position, "partial_exit", bar_row)
        # PickMyTradeExecutor returns None (async fill); fall back to bar close for display
        partial_price = _exit_fill.fill_price if _exit_fill else float(bar.Close)
        pnl = _compute_pnl(_position, partial_price)
        print(json.dumps({
            "signal_type": "PARTIAL_EXIT",
            "direction":   _position["direction"],
            "price":       round(float(partial_price), 4),
            "pnl":         round(float(pnl), 2),
        }), flush=True)
        POSITION_FILE.write_text(json.dumps(_position, indent=2))
        return

    if result == "exit_session_end":
        exit_price = float(bar.Close)
    elif result in (
        "exit_tp", "exit_secondary", "exit_stop",
        "exit_market",
        "exit_invalidation_mss", "exit_invalidation_cisd",
        "exit_invalidation_smt", "exit_invalidation_opposing_disp",
    ):
        _exit_fill = _executor.place_exit(_position, result, bar_row)
        # PickMyTradeExecutor returns None (async fill); fall back to bar close for display
        exit_price = _exit_fill.fill_price if _exit_fill else float(bar.Close)
    else:
        # Unknown exit type — log and bail rather than corrupt state with unbound exit_price
        return

    pnl = _compute_pnl(_position, exit_price)
    print(_format_exit_line(bar_ts, result, exit_price, pnl, _position["contracts"], _position["entry_time"]), flush=True)
    print(json.dumps({
        "signal_type":  "EXIT",
        "exit_reason":  result,
        "direction":    _position["direction"],
        "exit_price":   round(float(exit_price), 4),
        "entry_price":  round(float(_position["assumed_entry"]), 4),
        "pnl_dollars":  round(float(pnl), 4),
        "contracts":    _position["contracts"],
    }), flush=True)

    # Finalize hypothesis at session end (called before _position is cleared)
    if _hypothesis_manager is not None and result != "hold":
        exit_result_dict = {"exit_type": result, "pnl": pnl}
        _hypothesis_manager.finalize(_position, exit_result_dict)

    _last_exit_ts = bar_ts
    POSITION_FILE.unlink(missing_ok=True)
    # Reset scanner atomically so no stale pending fields survive into the next scan.
    # prior_trade_bars_held is preserved across the reset (computed from entry time).
    if _scan_state is not None:
        try:
            _entry_ts = pd.Timestamp(_position["entry_time"])
            if _entry_ts.tz is None:
                _entry_ts = _entry_ts.tz_localize("America/New_York")
            _bars_held = max(0, int((bar_ts - _entry_ts).total_seconds()))
        except Exception:
            _bars_held = 0
        _scan_state.reset()
        _scan_state.prior_trade_bars_held = _bars_held
    _position = None
    _state = "SCANNING"


# ── v2 pipeline dispatcher ───────────────────────────────────────────────────

def _emit_v2_signal(sig: dict) -> None:
    """Print a v2 signal dict as a JSON line to stdout (mirrors _dispatch_event for v2)."""
    print(json.dumps(sig, sort_keys=True), flush=True)


class SmtV2Dispatcher:
    """Routes IB bar callbacks through the v2 per-bar module pipeline.

    Selected when SMT_PIPELINE=v2. Accumulates 1m bars to build 5m bars at round
    boundaries and invokes hypothesis → trend → strategy in order per spec.
    """

    def __init__(self, mnq_1m_df: pd.DataFrame, mes_1m_df: pd.DataFrame,
                 hist_mnq_1m: pd.DataFrame, hist_mes_1m: pd.DataFrame) -> None:
        import daily as _daily_mod
        import hypothesis as _hyp_mod
        import strategy as _strat_mod
        import trend as _trend_mod
        self._daily = _daily_mod
        self._hyp = _hyp_mod
        self._strat = _strat_mod
        self._trend = _trend_mod
        self._mnq_1m_df = mnq_1m_df
        self._mes_1m_df = mes_1m_df
        self._hist_mnq_1m = hist_mnq_1m
        self._hist_mes_1m = hist_mes_1m
        self._daily_triggered = False
        self._session_date = None

    def on_session_start(self, now: pd.Timestamp) -> None:
        """Call once at 09:20 ET each session to run daily computations."""
        from smt_state import save_global, save_daily, save_hypothesis, save_position
        from smt_state import DEFAULT_GLOBAL, DEFAULT_DAILY, DEFAULT_HYPOTHESIS, DEFAULT_POSITION
        import copy
        save_global(copy.deepcopy(DEFAULT_GLOBAL))
        save_daily(copy.deepcopy(DEFAULT_DAILY))
        save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))
        save_position(copy.deepcopy(DEFAULT_POSITION))
        hist_hourly = self._hist_mnq_1m.resample("1h", label="left").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna()
        today_bars = self._mnq_1m_df[self._mnq_1m_df.index.date == now.date()]
        self._daily.run_daily(now, today_bars, self._hist_mnq_1m, hist_hourly)
        self._daily_triggered = True
        self._session_date = now.date()

    def on_1m_bar(self, now: pd.Timestamp, mnq_bar_row: pd.Series, mes_bar_row: pd.Series) -> None:
        """Process one completed 1m bar."""
        if not self._daily_triggered:
            return
        bar_dict = {
            "time":  now.isoformat(),
            "open":  float(mnq_bar_row["Open"]),
            "high":  float(mnq_bar_row["High"]),
            "low":   float(mnq_bar_row["Low"]),
            "close": float(mnq_bar_row["Close"]),
        }
        today_mnq = self._mnq_1m_df[self._mnq_1m_df.index.date == now.date()]
        recent = today_mnq[today_mnq.index <= now]
        is_5m = now.minute % 5 == 0

        # Trend always runs first: validates existing hypothesis before a new one may form.
        trend_sig = self._trend.run_trend(now, bar_dict, recent)
        if trend_sig is not None:
            _emit_v2_signal(trend_sig)

        if is_5m:
            today_mes = self._mes_1m_df[self._mes_1m_df.index.date == now.date()]
            self._hyp.run_hypothesis(
                now, today_mnq, today_mes, self._hist_mnq_1m, self._hist_mes_1m
            )
            start_5m = now - pd.Timedelta(minutes=4)
            window = today_mnq.loc[start_5m:now]
            if not window.empty:
                mnq_5m_bar = {
                    "time":      now.isoformat(),
                    "open":      float(window.iloc[0]["Open"]),
                    "high":      float(window["High"].max()),
                    "low":       float(window["Low"].min()),
                    "close":     float(window.iloc[-1]["Close"]),
                    "body_high": max(float(window.iloc[0]["Open"]), float(window.iloc[-1]["Close"])),
                    "body_low":  min(float(window.iloc[0]["Open"]), float(window.iloc[-1]["Close"])),
                }
                strat_sig = self._strat.run_strategy(now, mnq_5m_bar, recent)
                if strat_sig is not None:
                    _emit_v2_signal(strat_sig)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _ib_source, _executor, _state, _position, _startup_ts
    global _session_start_time, _session_end_time
    global _mes_partial_1m
    global _hypothesis_manager, _hypothesis_generated
    global _hist_daily_df
    global _smtv2_pipeline, _smtv2_dispatcher

    BAR_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Parse session window into time objects used by callbacks
    _session_start_time = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
    _session_end_time   = pd.Timestamp(f"2000-01-01 {SIGNAL_SESSION_END}").time()

    _mes_partial_1m = None

    # Load historical 5m data for hypothesis rule engine
    _hist_mnq_df = _load_hist_mnq()
    today = pd.Timestamp.now(tz="America/New_York").date()
    _hypothesis_manager = HypothesisManager(pd.DataFrame(), _hist_mnq_df, today)
    _hist_daily_df = _hist_mnq_df  # reuse 5m hist; compute_pdh_pdl uses index.date
    _hypothesis_generated = False

    # v2 pipeline env gate (dispatcher wiring deferred per spec § "Out of Scope")
    _smtv2_pipeline = os.environ.get("SMT_PIPELINE", "v1")

    # Restore open position from disk if the process was restarted mid-trade
    if POSITION_FILE.exists():
        _position = json.loads(POSITION_FILE.read_text())
        _state = "MANAGING"
        _startup_ts = None
    else:
        _state = "SCANNING"
        # Startup guard timestamp: any signal entry_time <= this is considered stale
        _startup_ts = pd.Timestamp.now(tz="America/New_York")

    # Validate required env vars before connecting to IB
    required = ["PMT_WEBHOOK_URL", "PMT_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")

    today_str = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
    fills_path = SESSIONS_DIR / today_str / "fills.jsonl"
    fills_path.parent.mkdir(parents=True, exist_ok=True)
    _executor = PickMyTradeExecutor(
        webhook_url=os.environ["PMT_WEBHOOK_URL"],
        api_key=os.environ["PMT_API_KEY"],
        symbol=os.environ.get("TRADING_SYMBOL", "MNQ1!"),
        account_id=os.environ.get("TRADING_ACCOUNT_ID", ""),
        contracts=int(os.environ.get("TRADING_CONTRACTS", "1")),
        fill_mode=os.environ.get("PMT_FILL_MODE", "poll"),
        fill_poll_interval_s=int(os.environ.get("PMT_FILL_POLL_INTERVAL_S", "30")),
        fill_webhook_port=int(os.environ.get("PMT_FILL_WEBHOOK_PORT", "8765")),
        fills_path=fills_path,
        fills_url=os.environ.get("PMT_FILLS_URL", ""),
    )

    def _on_bar_1m_complete(bars) -> None:
        """Called by IbRealtimeSource after each completed 1m bar.

        Drives the hypothesis rule engine: generate at the first 1m bar at/after
        SESSION_START, then evaluate every subsequent bar.
        """
        global _hypothesis_generated
        if _hypothesis_manager is None:
            return
        bar_time = pd.Timestamp(getattr(bars[-1], "date", None) or bars[-1].name)
        if bar_time.tz is None:
            bar_time = bar_time.tz_localize("America/New_York")
        bar_time = bar_time.tz_convert("America/New_York").time()
        _session_start_time_local = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
        if not _hypothesis_generated and bar_time >= _session_start_time_local:
            _hypothesis_manager.generate()
            _hypothesis_generated = True
        _hypothesis_manager.evaluate_bar(bars[-1])

    _ib_source = IbRealtimeSource(
        host=IB_HOST, port=IB_PORT, client_id=IB_CLIENT_ID,
        mnq_conid=MNQ_CONID, mes_conid=MES_CONID,
        bar_data_dir=BAR_DATA_DIR,
        on_bar=_on_bar,
        max_retries=MAX_RETRIES, retry_delay_s=RETRY_DELAY_S,
        on_bar_1m_complete=_on_bar_1m_complete,
    )

    _executor.start()
    try:
        _ib_source.start()  # blocks; retry loop is inside IbRealtimeSource
    finally:
        _executor.stop()


if __name__ == "__main__":
    main()
