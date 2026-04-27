# signal_smt.py
# Realtime SMT divergence signal generator for MNQ1!/MES1!.
# Subscribes to dual 1m IB streams + reqTickByTickData("AllLast") per instrument; detects signals on each 1s bar
# via process_scan_bar, then manages the position through the session using manage_position.
"""
Usage: python signal_smt.py

Requires IB Gateway (or TWS) running at IB_HOST:IB_PORT with API enabled.
State machine: SCANNING → MANAGING, one trade per session.
"""
from pathlib import Path
import json
import math as _math
import time

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from ib_insync import IB, Future, util

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
# strategy_smt attributes in tests affects signal_smt without a re-import.
import strategy_smt
from hypothesis_smt import HypothesisManager

# ── Connection constants ──────────────────────────────────────────────────────
IB_HOST      = "127.0.0.1"
IB_PORT      = 4002
IB_CLIENT_ID = 15

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
POSITION_FILE = BAR_DATA_DIR / "position.json"

# ── Module-level state (set in main()) ───────────────────────────────────────
_ib: IB = None
_mnq_contract = None
_mes_contract = None

_mnq_1m_df: pd.DataFrame = None   # loaded from parquet + gap-filled
_mes_1m_df: pd.DataFrame = None
# Per-instrument partial 1m bar accumulators — OHLCV built from ticks since the current minute started
_mnq_partial_1m: dict | None = None
_mes_partial_1m: dict | None = None

# Per-instrument tick accumulator for the current in-progress second (MNQ only; used for second-boundary detection)
_mnq_tick_bar: dict | None = None

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_bar_df() -> pd.DataFrame:
    """Return an empty OHLCV DataFrame with a tz-aware ET DatetimeIndex."""
    return pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )


def _apply_slippage(signal: dict) -> float:
    """Compute assumed fill price after adverse slippage.

    Slippage is applied against the trade: long pays more, short receives less.
    """
    if signal["direction"] == "long":
        return signal["entry_price"] + ENTRY_SLIPPAGE_TICKS * 0.25
    return signal["entry_price"] - ENTRY_SLIPPAGE_TICKS * 0.25


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


def _update_tick_accumulator(
    acc: dict | None, price: float, size: float, second_ts: pd.Timestamp
) -> tuple[dict, dict | None]:
    """Update or create a tick accumulator for the current second.

    Returns (new_acc, finalized_acc_or_None). A non-None finalized value means
    a second boundary was crossed and the returned acc is the finalized bar.
    """
    if acc is None or second_ts != acc["second_ts"]:
        # Second boundary crossed — finalize old accumulator, start a new one
        finalized = acc
        new_acc = {"open": price, "high": price, "low": price,
                   "close": price, "volume": size, "second_ts": second_ts}
        return new_acc, finalized
    # Same second — update running OHLCV
    acc["high"]   = max(acc["high"], price)
    acc["low"]    = min(acc["low"], price)
    acc["close"]  = price
    acc["volume"] += size
    return acc, None


def _update_partial_1m(acc: dict | None, price: float, size: float, minute_ts: pd.Timestamp) -> dict:
    """Update or create the partial 1m bar accumulator for the current minute.

    On minute boundary the accumulator resets; within a minute it tracks running OHLCV
    since the first tick of that minute.
    """
    if acc is None or minute_ts != acc["minute_ts"]:
        return {"open": price, "high": price, "low": price, "close": price, "volume": size, "minute_ts": minute_ts}
    acc["high"]   = max(acc["high"], price)
    acc["low"]    = min(acc["low"], price)
    acc["close"]  = price
    acc["volume"] += size
    return acc


def _partial_1m_to_bar_row(acc: dict, ts: pd.Timestamp) -> "strategy_smt._BarRow":
    """Build a _BarRow from a partial 1m accumulator, stamped with the current second boundary."""
    return strategy_smt._BarRow(acc["open"], acc["high"], acc["low"], acc["close"], acc["volume"], ts)


def _bar_timestamp(bar) -> pd.Timestamp:
    # IB bars expose .date; strategy_smt._BarRow exposes .name
    ts = pd.Timestamp(getattr(bar, "date", None) or bar.name)
    if ts.tz is None:
        return ts.tz_localize("America/New_York")
    return ts.tz_convert("America/New_York")


def _load_hist_mnq() -> pd.DataFrame:
    """Load the Databento 5m historical parquet for hypothesis rule engine."""
    hist_path = Path("data/historical/MNQ.parquet")
    if hist_path.exists():
        return pd.read_parquet(hist_path)
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def _load_parquets() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load cached 1m parquet files. Returns empty DataFrames if files don't exist."""
    mnq_path = BAR_DATA_DIR / "MNQ_1m.parquet"
    mes_path  = BAR_DATA_DIR / "MES_1m.parquet"
    mnq_df = pd.read_parquet(mnq_path) if mnq_path.exists() else _empty_bar_df()
    mes_df = pd.read_parquet(mes_path) if mes_path.exists() else _empty_bar_df()
    return mnq_df, mes_df


def _gap_fill_1m(
    mnq_df: pd.DataFrame, mes_df: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch any missing 1m bars from IB and append to the DataFrames.

    Uses a separate IB client ID to avoid conflicts with the live subscription connection.
    Caps the lookback at MAX_LOOKBACK_DAYS regardless of parquet age.
    """
    from data.sources import IBGatewaySource

    now = pd.Timestamp.now(tz="America/New_York")

    # Compute per-instrument start timestamps independently so that an empty MES
    # parquet still gets the full 30-day fill even when MNQ is already populated.
    def _start_ts_for(df: pd.DataFrame) -> pd.Timestamp:
        gap_days = MAX_LOOKBACK_DAYS if df.empty else GAP_FILL_MAX_DAYS
        floor = now - pd.Timedelta(days=gap_days)
        return max(df.index[-1], floor) if not df.empty else floor

    mnq_start_str = _start_ts_for(mnq_df).isoformat()
    mes_start_str = _start_ts_for(mes_df).isoformat()
    end_str = now.isoformat()

    # Use a different client ID for the gap-fill fetch (avoids clash with main IB connection)
    source = IBGatewaySource(host=IB_HOST, port=IB_PORT, client_id=IB_CLIENT_ID + 1)

    mnq_new = source.fetch(MNQ_CONID, mnq_start_str, end_str, interval="1m", contract_type="future_by_conid")
    mes_new = source.fetch(MES_CONID, mes_start_str, end_str, interval="1m", contract_type="future_by_conid")

    if mnq_new is not None and not mnq_new.empty:
        mnq_df = pd.concat([mnq_df, mnq_new]).sort_index()
        mnq_df = mnq_df[~mnq_df.index.duplicated(keep="last")]

    if mes_new is not None and not mes_new.empty:
        mes_df = pd.concat([mes_df, mes_new]).sort_index()
        mes_df = mes_df[~mes_df.index.duplicated(keep="last")]

    # Persist updated caches
    BAR_DATA_DIR.mkdir(parents=True, exist_ok=True)
    mnq_df.to_parquet(BAR_DATA_DIR / "MNQ_1m.parquet")
    mes_df.to_parquet(BAR_DATA_DIR / "MES_1m.parquet")

    return mnq_df, mes_df


# ── IB bar callbacks ──────────────────────────────────────────────────────────

def on_mnq_1m_bar(bars, hasNewBar):
    """Fired when a new completed 1m MNQ bar arrives.

    Appends the bar to the persistent 1m DataFrame, saves the parquet,
    and clears the 1s buffer and tick accumulator so the next minute starts fresh.
    """
    global _mnq_1m_df, _mnq_tick_bar
    if not hasNewBar:
        return
    bar = bars[-1]
    bar_ts = _bar_timestamp(bar)
    row = pd.DataFrame(
        [[float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)]],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([bar_ts]),
    )
    _mnq_1m_df = pd.concat([_mnq_1m_df, row])
    _mnq_1m_df = _mnq_1m_df[~_mnq_1m_df.index.duplicated(keep="last")]
    _mnq_1m_df.to_parquet(BAR_DATA_DIR / "MNQ_1m.parquet")
    # Reset second accumulator so the last second of the expiring minute does not
    # bleed into the next minute's second boundary detection.
    _mnq_tick_bar = None
    set_bar_data(_mnq_1m_df, _mes_1m_df)

    # Hypothesis: generate on first 1m bar at/after 09:00 ET; evaluate every bar
    global _hypothesis_manager, _hypothesis_generated
    if _hypothesis_manager is not None:
        bar_time = _bar_timestamp(bars[-1]).time()
        _session_start_time_local = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
        if not _hypothesis_generated and bar_time >= _session_start_time_local:
            _hypothesis_manager.generate()
            _hypothesis_generated = True
        _hypothesis_manager.evaluate_bar(bars[-1])


def on_mes_1m_bar(bars, hasNewBar):
    """Fired when a new completed 1m MES bar arrives.

    Appends to the persistent 1m DataFrame and saves the parquet.
    """
    global _mes_1m_df
    if not hasNewBar:
        return
    bar = bars[-1]
    bar_ts = _bar_timestamp(bar)
    row = pd.DataFrame(
        [[float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)]],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([bar_ts]),
    )
    _mes_1m_df = pd.concat([_mes_1m_df, row])
    _mes_1m_df = _mes_1m_df[~_mes_1m_df.index.duplicated(keep="last")]
    _mes_1m_df.to_parquet(BAR_DATA_DIR / "MES_1m.parquet")
    set_bar_data(_mnq_1m_df, _mes_1m_df)


def _tick_second_ts(t) -> pd.Timestamp:
    """Convert an IB tick timestamp to an ET-localized second boundary.

    Guards against tz-naive timestamps, which IB can send on reconnect.
    """
    ts = pd.Timestamp(t.time)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("America/New_York").floor("s")


def on_mes_tick(ticker) -> None:
    """Fired on each trade tick for MES via reqTickByTickData("AllLast").

    Updates the partial 1m bar accumulator with each tick. MES is passive — never
    calls _process(); the MNQ tick handler drives session scanning.
    """
    global _mes_partial_1m
    if not ticker.tickByTicks:
        return
    t = ticker.tickByTicks[-1]
    second_ts = _tick_second_ts(t)
    minute_ts = second_ts.floor("min")
    _mes_partial_1m = _update_partial_1m(_mes_partial_1m, t.price, t.size, minute_ts)


def on_mnq_tick(ticker) -> None:
    """Fired on each trade tick for MNQ via reqTickByTickData("AllLast").

    On each second boundary, fires _process() with the current partial 1m bar
    (OHLCV accumulated since the start of the current minute, stamped at the
    completed second). The partial bar is updated with the triggering tick after
    firing so _process sees the bar's state at the end of the completed second.
    """
    global _mnq_tick_bar, _mnq_partial_1m
    if not ticker.tickByTicks:
        return
    t = ticker.tickByTicks[-1]
    second_ts = _tick_second_ts(t)
    minute_ts = second_ts.floor("min")
    _mnq_tick_bar, finalized = _update_tick_accumulator(_mnq_tick_bar, t.price, t.size, second_ts)
    if finalized is not None and _mnq_partial_1m is not None:
        _process(_partial_1m_to_bar_row(_mnq_partial_1m, finalized["second_ts"]))
    _mnq_partial_1m = _update_partial_1m(_mnq_partial_1m, t.price, t.size, minute_ts)


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

    # 2. Alignment gate: both partial 1m bars must exist and be for the same minute
    if _mes_partial_1m is None or _mnq_partial_1m is None:
        return
    if _mes_partial_1m["minute_ts"] != _mnq_partial_1m["minute_ts"]:
        return

    today = bar_ts.date()

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

    # 11. Open position with slippage-adjusted assumed fill
    assumed_entry = _apply_slippage(signal)
    if strategy_smt.HUMAN_EXECUTION_MODE and strategy_smt.HUMAN_ENTRY_SLIPPAGE_PTS > 0:
        # Additive human-reaction slippage for market fills (direction-correct).
        if signal["direction"] == "long":
            assumed_entry += strategy_smt.HUMAN_ENTRY_SLIPPAGE_PTS
        else:
            assumed_entry -= strategy_smt.HUMAN_ENTRY_SLIPPAGE_PTS
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

    if result == "exit_tp":
        exit_price = _position["take_profit"]
    elif result == "exit_secondary":
        # Limit order hit at secondary target — fills at defined price
        exit_price = _position["secondary_target"]
    elif result == "exit_stop":
        exit_price = _position["stop_price"]
    elif result in (
        "exit_market",
        "exit_invalidation_mss",
        "exit_invalidation_cisd",
        "exit_invalidation_smt",
        "exit_invalidation_opposing_disp",
    ):
        exit_price = float(bar.Close)
    elif result != "exit_session_end":
        # Unknown exit type — log and bail rather than corrupt state with unbound exit_price
        return

    pnl = _compute_pnl(_position, exit_price)
    print(_format_exit_line(bar_ts, result, exit_price, pnl, _position["contracts"], _position["entry_time"]), flush=True)

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


# ── IB subscription setup ─────────────────────────────────────────────────────

def _setup_ib_subscriptions(ib: IB, mnq_contract, mes_contract) -> None:
    """Register all 4 IB data subscriptions and wire their event callbacks.

    Called on every connection attempt so subscriptions are re-registered after
    a disconnect; ib_insync does not automatically re-deliver them on reconnect.
    """
    mnq_1m = ib.reqHistoricalData(
        mnq_contract, endDateTime="", durationStr="1 D",
        barSizeSetting="1 min", whatToShow="TRADES",
        useRTH=False, formatDate=2, keepUpToDate=True,
    )
    mes_1m = ib.reqHistoricalData(
        mes_contract, endDateTime="", durationStr="1 D",
        barSizeSetting="1 min", whatToShow="TRADES",
        useRTH=False, formatDate=2, keepUpToDate=True,
    )
    mnq_tick = ib.reqTickByTickData(mnq_contract, "AllLast", 0, False)
    mes_tick  = ib.reqTickByTickData(mes_contract, "AllLast", 0, False)
    mnq_1m.updateEvent   += on_mnq_1m_bar
    mes_1m.updateEvent   += on_mes_1m_bar
    mnq_tick.updateEvent += on_mnq_tick
    mes_tick.updateEvent += on_mes_tick


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _ib, _mnq_contract, _mes_contract, _mnq_1m_df, _mes_1m_df
    global _mnq_partial_1m, _mes_partial_1m, _state, _position, _startup_ts
    global _session_start_time, _session_end_time
    global _mnq_tick_bar
    global _hypothesis_manager, _hypothesis_generated
    global _hist_daily_df

    BAR_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # This module is only used for signalling to a human trader, so human-mode
    # classification/emission is always on when signal_smt is the entry point.
    # Backtest imports strategy_smt directly and keeps the module default (False).
    strategy_smt.HUMAN_EXECUTION_MODE = True

    # Parse session window into time objects used by callbacks
    _session_start_time = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
    _session_end_time   = pd.Timestamp(f"2000-01-01 {SIGNAL_SESSION_END}").time()

    _mnq_partial_1m = None
    _mes_partial_1m = None
    _mnq_tick_bar = None

    # Load and gap-fill the persistent 1m parquet caches
    _mnq_1m_df, _mes_1m_df = _load_parquets()
    _mnq_1m_df, _mes_1m_df = _gap_fill_1m(_mnq_1m_df, _mes_1m_df)

    # Load historical 5m data for hypothesis rule engine
    _hist_mnq_df = _load_hist_mnq()
    today = pd.Timestamp.now(tz="America/New_York").date()
    _hypothesis_manager = HypothesisManager(_mnq_1m_df, _hist_mnq_df, today)

    # Load historical daily data for PDH/PDL computation (Solution F)
    _hist_daily_df = _hist_mnq_df  # reuse 5m hist; compute_pdh_pdl uses index.date
    _hypothesis_generated = False

    # Restore open position from disk if the process was restarted mid-trade
    if POSITION_FILE.exists():
        _position = json.loads(POSITION_FILE.read_text())
        _state = "MANAGING"
        _startup_ts = None
    else:
        _state = "SCANNING"
        # Startup guard timestamp: any signal entry_time <= this is considered stale
        _startup_ts = pd.Timestamp.now(tz="America/New_York")

    # Contracts are stateless; create once outside the retry loop
    mnq_contract = Future(conId=int(MNQ_CONID), exchange="CME")
    mes_contract = Future(conId=int(MES_CONID), exchange="CME")

    # Retry loop: handles startup clientId conflicts (RC3) and IB Gateway restarts (RC2).
    # Module-level state (_mnq_1m_df, _state, _position, etc.) is preserved across retries.
    for attempt in range(MAX_RETRIES):
        try:
            _ib = IB()
            _ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
            _setup_ib_subscriptions(_ib, mnq_contract, mes_contract)
            util.run()
            # util.run() exits normally on unexpected IB disconnects (e.g. error 1100/10141);
            # only treat it as a clean exit if the connection is still live.
            if _ib.isConnected():
                break
            raise ConnectionError("IB disconnected unexpectedly")
        except Exception as exc:
            print(
                f"[{attempt + 1}/{MAX_RETRIES}] IB connection error: {exc}. "
                f"Retrying in {RETRY_DELAY_S}s ...",
                flush=True,
            )
            try:
                if _ib and _ib.isConnected():
                    _ib.disconnect()
            except Exception:
                pass
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)

    else:
        # for-loop completed without break → all retry attempts exhausted
        print(f"[FATAL] Failed to connect after {MAX_RETRIES} attempts. Exiting.", flush=True)

    # Graceful shutdown after loop exits (clean break or exhausted retries)
    try:
        if _ib and _ib.isConnected():
            _ib.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
