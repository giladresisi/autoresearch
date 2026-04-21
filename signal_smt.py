# signal_smt.py
# Realtime SMT divergence signal generator for MNQ1!/MES1!.
# Subscribes to dual 1m IB streams + reqTickByTickData("AllLast") per instrument; detects signals on each 1s bar
# via screen_session, then manages the position through the session using manage_position.
"""
Usage: python signal_smt.py

Requires IB Gateway (or TWS) running at IB_HOST:IB_PORT with API enabled.
State machine: SCANNING → MANAGING, one trade per session.
"""
from pathlib import Path
import json
import time

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from ib_insync import IB, Future, util

from strategy_smt import (
    screen_session, manage_position, compute_tdo, set_bar_data,
    compute_midnight_open, compute_overnight_range,
    compute_pdh_pdl, select_draw_on_liquidity,
    detect_fvg, detect_displacement, detect_smt_fill,
    MIDNIGHT_OPEN_AS_TP, OVERNIGHT_SWEEP_REQUIRED, OVERNIGHT_RANGE_AS_TP,
    MIN_RR_FOR_TARGET, MIN_TARGET_PTS,
    MAX_REENTRY_COUNT,
    divergence_score, _effective_div_score,
    MIN_DIV_SCORE, REPLACE_THRESHOLD,
    DIV_SCORE_DECAY_FACTOR, DIV_SCORE_DECAY_INTERVAL,
    ADVERSE_MOVE_FULL_DECAY_PTS, ADVERSE_MOVE_MIN_DECAY,
    HYPOTHESIS_INVALIDATION_PTS,
)
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

# ── Session window (caller-side only; never passed to screen_session) ─────────
SESSION_START = "09:00"   # ET
SESSION_END   = "13:30"   # ET

# ── Trade configuration ───────────────────────────────────────────────────────
# 2-tick adverse fill; 1 tick = 0.25 MNQ points
ENTRY_SLIPPAGE_TICKS = 2
MAX_LOOKBACK_DAYS    = 30
GAP_FILL_MAX_DAYS    = 3   # cap pacing load at startup; 30-day history already in parquet
MNQ_PNL_PER_POINT    = 2.0

# ── Reconnect settings ────────────────────────────────────────────────────────
MAX_RETRIES   = 10
RETRY_DELAY_S = 15

# ── Realtime data paths ───────────────────────────────────────────────────────
REALTIME_DATA_DIR = Path("data/realtime")
POSITION_FILE     = REALTIME_DATA_DIR / "position.json"

# ── Module-level state (set in main()) ───────────────────────────────────────
_ib: IB = None
_mnq_contract = None
_mes_contract = None

_mnq_1m_df: pd.DataFrame = None   # loaded from parquet + gap-filled
_mes_1m_df: pd.DataFrame = None
_mnq_1s_buf: pd.DataFrame = None  # current-minute 1s bars, cleared on each new 1m bar
_mes_1s_buf: pd.DataFrame = None

# Per-instrument tick accumulators for the current in-progress second
_mnq_tick_bar: dict | None = None   # running OHLCV accumulator for in-progress second
_mes_tick_bar: dict | None = None

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

# Derived time objects (set from SESSION_START/END strings in main())
_session_start_time = None
_session_end_time   = None


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
    return (
        f"[{ts.strftime('%H:%M:%S')}] SIGNAL    {signal['direction']:<5} | "
        f"entry_time {entry_time.strftime('%H:%M:%S')} | "
        f"entry ~{assumed_entry:.2f} ({slip_label}) | "
        f"stop {signal['stop_price']:.2f} | "
        f"TP {signal['take_profit']:.2f} | "
        f"RR ~{rr:.1f}x"
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


def _build_1s_buffer_df() -> pd.DataFrame:
    """Factory alias for _empty_bar_df; used in tests to verify buffer schema."""
    return _empty_bar_df()


class _SyntheticBar:
    """Minimal bar-like object built from a finalized tick accumulator.

    Allows on_mnq_tick to pass a finalized second's data to _process()
    without modifying _process(), _process_scanning(), or _process_managing().
    """
    __slots__ = ("date", "open", "high", "low", "close", "volume")

    def __init__(self, acc: dict) -> None:
        self.date   = acc["second_ts"]
        self.open   = acc["open"]
        self.high   = acc["high"]
        self.low    = acc["low"]
        self.close  = acc["close"]
        self.volume = acc["volume"]


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


def _acc_to_df_row(acc: dict) -> pd.DataFrame:
    """Convert a finalized tick accumulator to a one-row OHLCV DataFrame.

    Output schema is identical to _append_1s_bar: columns Open/High/Low/Close/Volume,
    ET-localized DatetimeIndex.
    """
    return pd.DataFrame(
        [[acc["open"], acc["high"], acc["low"], acc["close"], acc["volume"]]],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([acc["second_ts"]]),
    )


def _append_1s_bar(buf: pd.DataFrame, bar) -> pd.DataFrame:
    """Append an ib_insync bar object to a 1s buffer DataFrame.

    Returns the updated buffer. Caller must reassign the module variable.
    """
    bar_ts = _bar_timestamp(bar)
    row = pd.DataFrame(
        [[float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)]],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([bar_ts]),
    )
    return pd.concat([buf, row])


def _bar_timestamp(bar) -> pd.Timestamp:
    # bar.date from ib_insync is typically a naive datetime; localize if needed
    ts = pd.Timestamp(bar.date)
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
    mnq_path = REALTIME_DATA_DIR / "MNQ_1m.parquet"
    mes_path  = REALTIME_DATA_DIR / "MES_1m.parquet"
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
    REALTIME_DATA_DIR.mkdir(parents=True, exist_ok=True)
    mnq_df.to_parquet(REALTIME_DATA_DIR / "MNQ_1m.parquet")
    mes_df.to_parquet(REALTIME_DATA_DIR / "MES_1m.parquet")

    return mnq_df, mes_df


# ── IB bar callbacks ──────────────────────────────────────────────────────────

def on_mnq_1m_bar(bars, hasNewBar):
    """Fired when a new completed 1m MNQ bar arrives.

    Appends the bar to the persistent 1m DataFrame, saves the parquet,
    and clears the 1s buffer and tick accumulator so the next minute starts fresh.
    """
    global _mnq_1m_df, _mnq_1s_buf, _mnq_tick_bar
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
    _mnq_1m_df.to_parquet(REALTIME_DATA_DIR / "MNQ_1m.parquet")
    _mnq_1s_buf = _empty_bar_df()
    # Reset tick accumulator so the last second of the expiring minute is not
    # re-appended to the fresh buffer on the first tick of the next minute.
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
    global _mes_1m_df, _mes_1s_buf, _mes_tick_bar
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
    _mes_1m_df.to_parquet(REALTIME_DATA_DIR / "MES_1m.parquet")
    _mes_1s_buf = _empty_bar_df()
    # Reset tick accumulator alongside 1s buffer to avoid stale second bleeding
    # into the next minute's buffer on the first tick.
    _mes_tick_bar = None
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

    Accumulates ticks into per-second OHLCV. Appends the completed bar to
    _mes_1s_buf when the first tick of a new second arrives (boundary crossing).
    MES is passive — never calls _process().
    """
    global _mes_tick_bar, _mes_1s_buf
    if not ticker.tickByTicks:
        return
    t = ticker.tickByTicks[-1]
    second_ts = _tick_second_ts(t)
    _mes_tick_bar, finalized = _update_tick_accumulator(_mes_tick_bar, t.price, t.size, second_ts)
    if finalized is not None:
        _mes_1s_buf = pd.concat([_mes_1s_buf, _acc_to_df_row(finalized)])


def on_mnq_tick(ticker) -> None:
    """Fired on each trade tick for MNQ via reqTickByTickData("AllLast").

    Accumulates ticks into per-second OHLCV. On second boundary crossing,
    appends the completed bar to _mnq_1s_buf and triggers _process() for
    signal detection / position management.
    """
    global _mnq_tick_bar, _mnq_1s_buf
    if not ticker.tickByTicks:
        return
    t = ticker.tickByTicks[-1]
    second_ts = _tick_second_ts(t)
    _mnq_tick_bar, finalized = _update_tick_accumulator(_mnq_tick_bar, t.price, t.size, second_ts)
    if finalized is not None:
        _mnq_1s_buf = pd.concat([_mnq_1s_buf, _acc_to_df_row(finalized)])
        _process(_SyntheticBar(finalized))


# ── State machine ─────────────────────────────────────────────────────────────

def _process(bar) -> None:
    """Route each 1s bar to the appropriate state handler."""
    bar_ts   = _bar_timestamp(bar)
    bar_time = bar_ts.time()

    if _state == "SCANNING":
        _process_scanning(bar, bar_ts, bar_time)
    else:
        _process_managing(bar, bar_ts, bar_time)


def _process_scanning(bar, bar_ts: pd.Timestamp, bar_time) -> None:
    """SCANNING state: check all gates then attempt signal detection on each 1s bar."""
    global _state, _position, _last_exit_ts
    global _current_session_date, _current_divergence_level, _divergence_reentry_count
    global _day_pdh, _day_pdl, _pdh_pdl_date

    # 1. Session gate: only scan during kill zone
    if bar_time < _session_start_time or bar_time > _session_end_time:
        return

    # 1a. Hypothesis gate: do not scan until generate() has run for today's session
    if _hypothesis_manager is not None and not _hypothesis_generated:
        return

    # 2. Alignment gate: MES 1s buffer must be current (same latest ts as MNQ)
    if _mes_1s_buf.empty or _mes_1s_buf.index[-1] != _mnq_1s_buf.index[-1]:
        return

    # 3. Build combined 1m + 1s DataFrames for signal detection
    combined_mnq = pd.concat([_mnq_1m_df, _mnq_1s_buf]).sort_index() if not _mnq_1s_buf.empty else _mnq_1m_df
    combined_mes = pd.concat([_mes_1m_df, _mes_1s_buf]).sort_index() if not _mes_1s_buf.empty else _mes_1m_df

    # 4. Slice to today's session window
    today = bar_ts.date()
    session_mask = (
        (combined_mnq.index.date == today)
        & (combined_mnq.index.time >= _session_start_time)
        & (combined_mnq.index.time <= _session_end_time)
    )
    mnq_session = combined_mnq[session_mask]
    mes_session = combined_mes[
        (combined_mes.index.date == today)
        & (combined_mes.index.time >= _session_start_time)
        & (combined_mes.index.time <= _session_end_time)
    ]

    # 5. Compute TDO from the full 1m parquet (needs the midnight bar)
    tdo = compute_tdo(_mnq_1m_df, today)
    if tdo is None:
        return

    midnight_open_price = compute_midnight_open(_mnq_1m_df, today) if MIDNIGHT_OPEN_AS_TP else None
    overnight_range = (
        compute_overnight_range(_mnq_1m_df, today)
        if (OVERNIGHT_SWEEP_REQUIRED or OVERNIGHT_RANGE_AS_TP)
        else None
    )

    # Compute PDH/PDL once per session day (Solution F)
    if today != _pdh_pdl_date and _hist_daily_df is not None:
        _day_pdh, _day_pdl = compute_pdh_pdl(_hist_daily_df, today)
        _pdh_pdl_date = today

    # 6. Detect signal
    signal = screen_session(mnq_session, mes_session, tdo,
                            midnight_open=midnight_open_price,
                            overnight_range=overnight_range)
    if signal is None:
        return

    # Apply draw-on-liquidity target selection (Solution F)
    _ep = signal["entry_price"]
    _sp = signal["stop_price"]
    _direction = signal["direction"]
    _ses_high = float(mnq_session["High"].max()) if not mnq_session.empty else 0.0
    _ses_low  = float(mnq_session["Low"].min())  if not mnq_session.empty else float("inf")
    _fvg_high = signal.get("fvg_high")
    _fvg_low  = signal.get("fvg_low")
    _ovn_high = overnight_range.get("overnight_high") if overnight_range else None
    _ovn_low  = overnight_range.get("overnight_low")  if overnight_range else None
    if _direction == "long":
        _draws = {
            "fvg_top":        _fvg_high if _fvg_high and _fvg_high > _ep else None,
            "tdo":            tdo if tdo and tdo > _ep else None,
            "midnight_open":  midnight_open_price if midnight_open_price and midnight_open_price > _ep else None,
            "session_high":   _ses_high if _ses_high > _ep + 1 else None,
            "overnight_high": _ovn_high if _ovn_high and _ovn_high > _ep else None,
            "pdh":            _day_pdh if _day_pdh and _day_pdh > _ep else None,
        }
    else:
        _draws = {
            "fvg_bottom":    _fvg_low if _fvg_low and _fvg_low < _ep else None,
            "tdo":           tdo if tdo and tdo < _ep else None,
            "midnight_open": midnight_open_price if midnight_open_price and midnight_open_price < _ep else None,
            "session_low":   _ses_low if _ses_low < _ep - 1 else None,
            "overnight_low": _ovn_low if _ovn_low and _ovn_low < _ep else None,
            "pdl":           _day_pdl if _day_pdl and _day_pdl < _ep else None,
        }
    _tp_name, _selected_tp, _sec_tp_name, _sec_tp = select_draw_on_liquidity(
        _direction, _ep, _sp, _draws, MIN_RR_FOR_TARGET, MIN_TARGET_PTS,
    )
    if _selected_tp is None:
        return  # no viable draw — skip this signal
    signal["take_profit"]  = _selected_tp
    signal["tp_name"]      = _tp_name

    # Annotate signal with hypothesis alignment
    if _hypothesis_manager is not None and _hypothesis_manager.direction_bias is not None:
        signal["matches_hypothesis"] = (
            signal.get("direction") == _hypothesis_manager.direction_bias
        )
    else:
        signal["matches_hypothesis"] = None

    # 7. Stale startup guard: skip signals that fired before this process started
    if _startup_ts is not None and signal["entry_time"] <= _startup_ts:
        return

    # 8. Re-detection guard: skip signals at or before the last exit timestamp
    if signal["entry_time"] <= _last_exit_ts:
        return

    # 8a. Session date reset: clear divergence tracking when a new trading day starts
    if today != _current_session_date:
        _current_session_date = today
        _current_divergence_level = None
        _divergence_reentry_count = 0

    # 8b. Per-divergence reentry guard: enforce MAX_REENTRY_COUNT on the live path
    defended = signal.get("smt_defended_level")
    if defended != _current_divergence_level:
        _current_divergence_level = defended
        _divergence_reentry_count = 0
    else:
        _divergence_reentry_count += 1
        if MAX_REENTRY_COUNT < 999 and _divergence_reentry_count >= MAX_REENTRY_COUNT:
            return

    # 9. Open position with slippage-adjusted assumed fill
    assumed_entry = _apply_slippage(signal)
    _position = {
        **signal,
        "assumed_entry": assumed_entry,
        "contracts": 1,
        "instrument": "MNQ1!",
        "entry_time": str(signal["entry_time"]),
        "secondary_target":      _sec_tp,
        "secondary_target_name": _sec_tp_name,
        "tp_breached":           False,
    }
    POSITION_FILE.write_text(json.dumps(_position, indent=2))
    print(_format_signal_line(bar_ts, signal, assumed_entry), flush=True)
    _state = "MANAGING"


def _process_managing(bar, bar_ts: pd.Timestamp, bar_time) -> None:
    """MANAGING state: check exit conditions on each 1s bar.

    Exits on TP, stop, or session end; prints exit line and resets to SCANNING.
    """
    global _state, _position, _last_exit_ts

    bar_series = pd.Series(
        {
            "Open":   float(bar.open),
            "High":   float(bar.high),
            "Low":    float(bar.low),
            "Close":  float(bar.close),
            "Volume": float(bar.volume),
        },
        name=bar_ts,
    )

    old_stop        = _position.get("stop_price")
    old_tp_breached = _position.get("tp_breached", False)
    old_breakeven   = _position.get("breakeven_active", False)

    result = manage_position(_position, bar_series)

    if result == "hold":
        if bar_time >= _session_end_time:
            # Force close at session end — no TP/stop triggered
            result = "exit_session_end"
            exit_price = float(bar.close)
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
    elif result in ("exit_market", "exit_invalidation_mss", "exit_invalidation_cisd", "exit_invalidation_smt"):
        exit_price = float(bar.close)
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
    global _mnq_1s_buf, _mes_1s_buf, _state, _position, _startup_ts
    global _session_start_time, _session_end_time
    global _mnq_tick_bar, _mes_tick_bar
    global _hypothesis_manager, _hypothesis_generated
    global _hist_daily_df

    REALTIME_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Parse session window into time objects used by callbacks
    _session_start_time = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
    _session_end_time   = pd.Timestamp(f"2000-01-01 {SESSION_END}").time()

    _mnq_1s_buf = _empty_bar_df()
    _mes_1s_buf = _empty_bar_df()
    _mnq_tick_bar = None
    _mes_tick_bar = None

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
            break  # clean exit — do not retry
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
