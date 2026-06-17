# trend.py
# Per-1m-bar cautious-mode management and trend-invalidation.
# Entry: run_trend(now, mnq_1m_bar, mnq_1m_recent) -> Optional[Signal]
# Reads hypothesis.json, position.json, daily.json.
# Writes hypothesis.json and position.json on state changes.

from __future__ import annotations

from datetime import datetime, time as _dtime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from smt_state import (
    load_daily_ro,
    load_global,
    load_hypothesis,
    load_position,
    save_global,
    save_hypothesis,
    save_position,
)

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------
Signal = dict


# ---------------------------------------------------------------------------
# O4: minimum buffer (pts) below fill_price before initial breakeven stop fires.
# Prevents 0-second stopouts when price oscillates around entry.
BREAKEVEN_BUFFER_PTS: float = 5.0

# Minimum distance (pts) between fill price and initial cautious level before
# any initial-level stop update fires (full arm OR wick-only midpoint).
# When the initial target is this close, touching it doesn't confirm the move.
INITIAL_STOP_MIN_DIST_PTS: float = 50.0

# ---------------------------------------------------------------------------
# O1 (GIL-34): survive the open. At the 9:30 open price oscillates right on the
# daily mid, so the binary close-vs-mid daily-mid invalidation kills every
# directional hypothesis ~2s after it forms (flat scan) and force-closes a fresh
# entry before any cautious level is reached (in-position) — no entry can arm
# even with the right direction. When ON, the *daily-mid* invalidation (both the
# flat-scan trend-broken and the unarmed in-position market-close) is suspended
# for bars whose ET wall-clock time falls inside the open window below. Weekly-mid
# and every other invalidation are untouched. Default OFF → byte-identical baseline.
OPEN_WINDOW_DAILY_MID_SUSPEND: bool = False
_OPEN_WINDOW_START_ET: _dtime = _dtime(9, 15)
_OPEN_WINDOW_END_ET:   _dtime = _dtime(11, 30)
_ET = ZoneInfo("America/New_York")


def _in_open_window(now: datetime) -> bool:
    """True when `now` (tz-aware → converted to ET; naive → assumed ET) is inside
    the [09:15, 11:30] ET open window. Mirrors session_pipeline's ET idiom."""
    ts = pd.Timestamp(now)
    et = ts.tz_convert(_ET) if ts.tzinfo is not None else ts
    return _OPEN_WINDOW_START_ET <= et.time() <= _OPEN_WINDOW_END_ET


# ---------------------------------------------------------------------------
# O2 (GIL-34) Rule 1 — secondary cautious = take-profit on touch, 09:30-09:45 ET.
# Targets the choppy short counter-move right at the 9:30 open (usually after a
# meaningful SMT, accurate but whippy): the moment the SECONDARY cautious level is
# touched (wick reach), market-close the position to bank the target instead of
# arming a protective stop and risking a give-back on the rejection. Initial-level
# protection is unchanged. The SMT-gated reverse entry (Rule 2) is intentionally
# NOT implemented here (deferred), so there is no phantom-fill exposure. Default
# OFF → byte-identical baseline.
SECONDARY_TP_ON_TOUCH_0930: bool = False
_SECONDARY_TP_START_ET: _dtime = _dtime(9, 30)
_SECONDARY_TP_END_ET:   _dtime = _dtime(9, 45)


def _in_secondary_tp_window(now: datetime) -> bool:
    """True when `now` (tz-aware → ET; naive → assumed ET) is inside [09:30, 09:45] ET."""
    ts = pd.Timestamp(now)
    et = ts.tz_convert(_ET) if ts.tzinfo is not None else ts
    return _SECONDARY_TP_START_ET <= et.time() <= _SECONDARY_TP_END_ET

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _market_close_signal(now: datetime, price: float, reason: str, close_reason: str = "") -> Signal:
    sig: Signal = {"kind": "market-close", "time": now.isoformat(), "price": price, "reason": reason}
    if close_reason:
        sig["close_reason"] = close_reason
    return sig


def _ref_bar_to_dict(ref: Optional[pd.Series]) -> dict:
    """Convert a pd.Series bar (from _last_same_dir_ref_bar) to a storable dict."""
    if ref is None:
        return {}
    ts = ref.name
    return {
        "time":      ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "open":      float(ref["Open"]),
        "high":      float(ref["High"]),
        "low":       float(ref["Low"]),
        "close":     float(ref["Close"]),
        "body_high": float(max(ref["Open"], ref["Close"])),
        "body_low":  float(min(ref["Open"], ref["Close"])),
    }


def _clear_position_and_hypothesis(
    position: dict, hypothesis: dict, *, clear_active: bool
) -> None:
    """Mutate position and hypothesis dicts in place — common cleanup for every market-close path."""
    if clear_active:
        position["active"] = {}
    position["stop_entry"] = ""
    position["conf_bar_entry"] = {}
    position["conf_bar_exit"]  = {}
    hypothesis["direction"] = "none"
    # GIL-8 invariant: clearing direction releases the manual direction lock too —
    # a stale lock with direction="none" would freeze future automatic resets.
    hypothesis["manual"] = False


def _last_same_dir_ref_bar(
    mnq_1m_recent: pd.DataFrame,
    current_bar_time: str,
    direction: str,
    period_minutes: int = 1,
) -> Optional[pd.Series]:
    """Most recent completed ref bar of period_minutes length whose body matches direction.

    period_minutes=1: raw 1m bars, skipping the bar at current_bar_time.
    period_minutes>1: aggregated bars, skipping bars not yet complete at current_bar_time.
    """
    if mnq_1m_recent.empty:
        return None

    try:
        current_ts = pd.Timestamp(current_bar_time)
    except Exception:
        current_ts = None

    def _matches(row: pd.Series) -> bool:
        return (direction == "down" and row["Close"] < row["Open"]) or \
               (direction == "up"   and row["Close"] > row["Open"])

    if period_minutes > 1:
        if current_ts is not None:
            if current_ts.tzinfo is not None and mnq_1m_recent.index.tz is None:
                current_ts = current_ts.tz_localize(None)
            elif current_ts.tzinfo is None and mnq_1m_recent.index.tz is not None:
                current_ts = current_ts.tz_localize(mnq_1m_recent.index.tz)
        _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        bars = mnq_1m_recent.resample(f"{period_minutes}min", label="left").agg(_agg).dropna(subset=["Open"])
        for i in range(len(bars) - 1, -1, -1):
            row = bars.iloc[i]
            row_ts = bars.index[i]
            if current_ts is not None and row_ts + pd.Timedelta(minutes=period_minutes) > current_ts:
                continue
            if _matches(row):
                return row
    else:
        for i in range(len(mnq_1m_recent) - 1, -1, -1):
            row = mnq_1m_recent.iloc[i]
            if current_ts is not None:
                row_ts = mnq_1m_recent.index[i]
                if current_ts.tzinfo is not None and row_ts.tzinfo is None:
                    row_ts = row_ts.tz_localize(current_ts.tzinfo)
                elif current_ts.tzinfo is None and row_ts.tzinfo is not None:
                    current_ts_naive = current_ts.tz_localize(None)
                    if row_ts == current_ts_naive:
                        continue
                if row_ts == current_ts:
                    continue
            if _matches(row):
                return row

    return None


def _arm_break_price(ref: Optional[pd.Series], direction: str) -> Optional[float]:
    """Body-high for direction='down', body-low for direction='up', of the reference bar."""
    if ref is None:
        return None
    return float(max(ref["Open"], ref["Close"])) if direction == "down" \
        else float(min(ref["Open"], ref["Close"]))


def _floored_break_price(
    ref: Optional[pd.Series], direction: str, fill_price: Optional[float],
    buffer_pts: float = 0.0,
) -> Optional[float]:
    """Like _arm_break_price but floors the result so the stop never moves against the position.

    buffer_pts: extra room beyond fill_price (O4 — prevents immediate breakeven stopouts).
    For longs (up): floor = fill_price - buffer_pts.  For shorts (down): floor = fill_price + buffer_pts.
    """
    price = _arm_break_price(ref, direction)
    if price is None or not fill_price:
        return price
    if direction == "up":
        return max(price, fill_price - buffer_pts)
    return min(price, fill_price + buffer_pts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_trend(
    now: datetime,
    mnq_1m_bar: dict,
    mnq_1m_recent: pd.DataFrame,
) -> Optional[Signal]:
    """Evaluate cautious-mode arming/rejection/exit and trend invalidation for one 1m bar.

    Parameters
    ----------
    now:
        Wall-clock / bar time for the current bar (used in signal timestamps).
    mnq_1m_bar:
        Dict with keys: "time", "open", "high", "low", "close".
    mnq_1m_recent:
        DataFrame of recent 1m bars (DatetimeIndex, columns Open/High/Low/Close),
        typically the last N bars including the current bar.

    Returns
    -------
    A Signal dict or None.
    """
    # ------------------------------------------------------------------
    # Step 1: load state.
    # ------------------------------------------------------------------
    hypothesis = load_hypothesis()
    position = load_position()
    daily = load_daily_ro()  # GIL-27: read-only (only reads level prices; never mutates daily)

    direction = hypothesis.get("direction", "none")
    # GIL-8 manual direction lock (trade.py set-direction): while set, the automatic
    # hypothesis resets below (global-trend / mid-cross trend-broken, session-ATH
    # cross) are suspended. Released via trade.py unlock / trend-broken, or by any
    # position-close path (_clear_position_and_hypothesis drops the flag).
    _manual_lock = bool(hypothesis.get("manual"))
    cautious_initial_raw   = hypothesis.get("cautious_price_initial",   "")
    cautious_secondary_raw = hypothesis.get("cautious_price_secondary", "")
    _lv1 = hypothesis.get("cautious_price_initial_level",   "") or ""
    _lv2 = hypothesis.get("cautious_price_secondary_level", "") or ""
    _cr1 = f"1st-cautious ({_lv1})" if _lv1 else "1st-cautious"
    _cr2 = f"2nd-cautious ({_lv2})" if _lv2 else "2nd-cautious"
    _liq_map = {l["name"]: l["price"] for l in daily.get("liquidities", [])
                if l.get("kind") == "level"}
    _dh = _liq_map.get("day_high")
    _dl = _liq_map.get("day_low")
    daily_mid_price = (_dh + _dl) / 2.0 if _dh is not None and _dl is not None else None
    _wh = _liq_map.get("week_high")
    _wl = _liq_map.get("week_low")
    weekly_mid_price = (_wh + _wl) / 2.0 if _wh is not None and _wl is not None else None

    # Guard: only apply mid-crossing invalidation when the hypothesis was formed
    # with price on the side consistent with the direction.  If direction=down was
    # set while price was already above the mid (e.g. Rule 2 approaching a low),
    # crossing the mid upward is expected behaviour, not invalidation.
    _hyp_daily_mid = hypothesis.get("daily_mid", "")
    _mid_cross_guard = (
        (direction == "up"   and _hyp_daily_mid in ("above", "mid")) or
        (direction == "down" and _hyp_daily_mid in ("below", "mid"))
    )
    _hyp_weekly_mid = hypothesis.get("weekly_mid", "")
    _weekly_mid_cross_guard = (
        (direction == "up"   and _hyp_weekly_mid in ("above", "mid")) or
        (direction == "down" and _hyp_weekly_mid in ("below", "mid"))
    )

    # O1 (GIL-34): suspend the daily-mid invalidation during the open window.
    # When the flag is OFF this is always False, so every guarded condition below
    # is byte-identical to master (and _in_open_window is never even evaluated).
    _suspend_daily_mid = OPEN_WINDOW_DAILY_MID_SUSPEND and _in_open_window(now)

    # ------------------------------------------------------------------
    # ATH maintenance: update dynamic all_time_high; detect two straddle types.
    # Runs on every bar regardless of direction so ATH stays current.
    # session_ath is seeded at 09:20 and never updated — it's the "original ATH"
    # that separates normal territory (below) from uncharted territory (above).
    # ------------------------------------------------------------------
    bar_high = float(mnq_1m_bar["high"])
    bar_low  = float(mnq_1m_bar["low"])
    _global_pre  = load_global()
    _session_ath = float(_global_pre.get("session_ath") or 0)
    _old_ath     = float(_global_pre.get("all_time_high") or _session_ath)
    # session_ath straddle: bar crossed the fixed 09:20 ATH — only relevant when
    # direction is not already "down" (we'd be crossing into uncharted territory).
    _session_ath_straddle = (
        _session_ath > 0
        and bar_high >= _session_ath
        and bar_low  <= _session_ath
        and direction not in ("down", "none")
        and not _manual_lock    # GIL-8: ATH cross must not reset a manual hypothesis
    )
    # Dynamic ATH straddle: above session_ath and price crossed the running high.
    # Triggers a lightweight hypothesis re-evaluation (no trend-broken).
    _dynamic_ath_straddle = (
        _old_ath > 0
        and bar_high >= _old_ath
        and bar_low  <= _old_ath
        and direction == "down"
    )
    if bar_high > _old_ath:
        _global_pre["all_time_high"] = bar_high
        save_global(_global_pre)

    # True only when the secondary cautious level is genuinely at ATH territory —
    # i.e. its price is at or above the pre-session ATH, meaning there is no
    # historical reference for how far price may run.
    _ath_secondary = (
        _lv2 in {"day_high", "week_high"}
        and _session_ath > 0
        and cautious_secondary_raw != ""
        and float(cautious_secondary_raw) >= _session_ath
    )

    # ------------------------------------------------------------------
    # SMT-v2 Phase 1: frozen active-position management snapshot.
    # When a position is open, Step 3 manages off the FROZEN snapshot captured at
    # fill (active["mgmt_direction"] + frozen cautious ladder), NOT the live, mutable
    # hypothesis direction / cautious_price_* fields. This decouples an open trade
    # from a hypothesis that may flip or go "none" while the trade rides — cautious
    # targets, not a force-close, decide the exit. Step 4 (flat scan) and the entry
    # path keep using the live hypothesis direction. (.agents/plans/
    # smt-v2-decouple-active-position.md)
    active = position.get("active", {})

    def _norm(d: str) -> str:  # long/short -> up/down; "" -> "none"
        return "up" if d == "long" else ("down" if d == "short" else (d or "none"))

    if active:
        # Resolve the frozen snapshot with back-compat fallback for positions written
        # before this change (lacking the frozen fields): mgmt_direction falls back to
        # the legacy active["direction"] (normalized), and the ladder falls back to the
        # live hypothesis cautious fields.
        mgmt_direction  = active.get("mgmt_direction") or _norm(active.get("direction", ""))
        f_initial_raw   = active.get("cautious_initial",   hypothesis.get("cautious_price_initial",   ""))
        f_secondary_raw = active.get("cautious_secondary", hypothesis.get("cautious_price_secondary", ""))
        f_lv1 = active.get("cautious_initial_level")   or hypothesis.get("cautious_price_initial_level",   "") or ""
        f_lv2 = active.get("cautious_secondary_level") or hypothesis.get("cautious_price_secondary_level", "") or ""
        f_cr1 = f"1st-cautious ({f_lv1})" if f_lv1 else "1st-cautious"
        f_cr2 = f"2nd-cautious ({f_lv2})" if f_lv2 else "2nd-cautious"
        # active-scoped ATH-secondary off the FROZEN lv2/secondary (mirror L267-272).
        f_ath_secondary = (
            f_lv2 in {"day_high", "week_high"}
            and _session_ath > 0
            and f_secondary_raw != ""
            and float(f_secondary_raw) >= _session_ath
        )
        # Mid-cross guards re-derived from mgmt_direction so they stay meaningful after a
        # flip (the live hypothesis daily_mid/weekly_mid side reference is still used; if
        # absent, default to applying the cross check — matching today when frozen==live).
        f_mid_cross_guard = (
            (mgmt_direction == "up"   and _hyp_daily_mid in ("above", "mid")) or
            (mgmt_direction == "down" and _hyp_daily_mid in ("below", "mid"))
        )
        f_weekly_mid_cross_guard = (
            (mgmt_direction == "up"   and _hyp_weekly_mid in ("above", "mid")) or
            (mgmt_direction == "down" and _hyp_weekly_mid in ("below", "mid"))
        )

    # ------------------------------------------------------------------
    # Step 2: early exit when no active direction.
    # ------------------------------------------------------------------
    # When a position is open it is managed off the frozen mgmt_direction (never
    # "none" for a real fill), so a "none" live hypothesis must NOT short-circuit
    # Step-3 management. Only return early when flat.
    if direction == "none" and not active:
        return None

    # Global trend invalidation: when confidence=high, cancel any hypothesis opposing global_trend.
    # Skipped while the manual direction lock is set (GIL-8) — the user forced this direction.
    # SMT-v2 Phase 1: also skipped while a position is open ("and not active") — this is a
    # hypothesis-level reset for the FLAT state; an open trade is managed by the frozen
    # snapshot + cautious exit, and the live hypothesis is free to reform on the next flat bar.
    _global_state = load_global()
    _global_trend = _global_state.get("trend", "up")
    if (_global_state.get("confidence") == "high" and direction != _global_trend
            and not _manual_lock and not active):
        hypothesis["direction"] = "none"
        position["conf_bar_entry"] = {}
        position["conf_bar_exit"]  = {}
        position["stop_entry"] = ""
        save_position(position)
        save_hypothesis(hypothesis)
        return {
            "kind":             "trend-broken",
            "time":             now.isoformat(),
            "direction":        direction,
            "broken_direction": direction,
            "level_name":       "global_trend",
            "level_price":      None,
            "price":            float(mnq_1m_bar.get("close", 0)),
        }

    # bar_high / bar_low already extracted in ATH block above.
    bar_open  = float(mnq_1m_bar["open"])
    bar_close = float(mnq_1m_bar["close"])
    bar_mid   = (bar_high + bar_low) / 2.0
    bar_time_str = str(mnq_1m_bar.get("time", now.isoformat()))

    # ------------------------------------------------------------------
    # Step 3: position is open.
    # ------------------------------------------------------------------
    # `active` and the frozen-snapshot resolver (mgmt_direction, f_initial_raw,
    # f_secondary_raw, f_lv1, f_lv2, f_cr1, f_cr2, f_ath_secondary, f_*mid_cross_guard)
    # were computed above (before the Step-2 gates). Everything below keys off the
    # FROZEN snapshot, NOT the live hypothesis. When frozen == live the management is
    # byte-equivalent to the pre-change behavior.
    if active:
        # Skip mid-minute bars (1s live/backtest): arm and break checks are
        # once-per-minute events; firing every second produces spurious exits.
        if pd.Timestamp(now).second != 0:
            return None

        # Re-key Step 3 to the frozen snapshot: shadow the live symbols so the
        # closures and break-checks below all manage off the frozen direction/ladder.
        direction               = mgmt_direction
        _lv1                    = f_lv1
        _lv2                    = f_lv2
        _cr1                    = f_cr1
        _cr2                    = f_cr2
        _ath_secondary          = f_ath_secondary
        _mid_cross_guard        = f_mid_cross_guard
        _weekly_mid_cross_guard = f_weekly_mid_cross_guard

        cautious_state = active.get("cautious", "no")
        _fill_price    = float(active.get("fill_price") or 0) or None

        cautious_initial   = float(f_initial_raw)   if f_initial_raw   != "" else None
        cautious_secondary = float(f_secondary_raw) if f_secondary_raw != "" else None

        def _surpassed(price: float) -> bool:
            return (bar_high >= price) if direction == "up" else (bar_low <= price)

        def _close_beyond(price: float) -> bool:
            return (bar_close > price) if direction == "up" else (bar_close < price)

        def _reversal(price: float) -> bool:
            return (bar_low <= price) if direction == "up" else (bar_high >= price)

        # ---- O2 (GIL-34) Rule 1: secondary take-profit on touch (09:30-09:45 ET) ----
        # Placed before the cautious_state branching so it applies uniformly in every
        # state (no / initial_surpassed / initial / secondary_surpassed / secondary):
        # the instant the SECONDARY level is touched (wick reach) inside the window,
        # market-close to bank the target. OFF default short-circuits before the window
        # check → byte-identical. No reverse entry here (deferred) → no phantom-fill.
        _secondary_tp_on_touch = (
            SECONDARY_TP_ON_TOUCH_0930
            and cautious_secondary is not None
            and _surpassed(cautious_secondary)
            and _in_secondary_tp_window(now)
        )
        if _secondary_tp_on_touch:
            _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
            save_position(position)
            save_hypothesis(hypothesis)
            return _market_close_signal(
                now, bar_mid, reason="secondary_tp_touch", close_reason="secondary-tp-touch")

        # ---- 3a: unarmed — check if a cautious level was reached -----------
        if cautious_state == "no":
            # Daily-mid invalidation: close crossed the mid against direction before any
            # cautious level was reached — the entry thesis is already broken.
            # O1 (GIL-34): suspended inside the open window when the flag is ON.
            if daily_mid_price is not None and _mid_cross_guard and not _suspend_daily_mid:
                _mid_broken = (direction == "up"   and bar_close < daily_mid_price) or \
                              (direction == "down" and bar_close > daily_mid_price)
                if _mid_broken:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return _market_close_signal(now, bar_mid, reason="daily_mid_cross", close_reason="daily-mid-cross")

            # Weekly-mid invalidation: same logic applied to the broader weekly range.
            if weekly_mid_price is not None and _weekly_mid_cross_guard:
                _wm_broken = (direction == "up"   and bar_close < weekly_mid_price) or \
                             (direction == "down" and bar_close > weekly_mid_price)
                if _wm_broken:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return _market_close_signal(now, bar_mid, reason="weekly_mid_cross", close_reason="weekly-mid-cross")

            if cautious_secondary is None and cautious_initial is None:
                return None

            # Secondary takes priority if surpassed (it's farther, confirms strong move).
            if cautious_secondary is not None and _surpassed(cautious_secondary):
                if _close_beyond(cautious_secondary):
                    _ref1 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction)
                    position["active"]["cautious"] = "secondary"
                    _sec_cbp = _fill_price if _ath_secondary else _floored_break_price(_ref1, direction, _fill_price)
                    position["active"]["cautious_break_price"] = _sec_cbp
                    position["conf_bar_exit"] = {} if _ath_secondary else _ref_bar_to_dict(_ref1)
                    save_position(position)
                    if position["active"]["cautious_break_price"] is None:
                        return None
                    return {"kind": "new-stop-exit", "time": now.isoformat(),
                            "price": _sec_cbp, "level": "secondary", "level_name": _lv2,
                            "cautious_break_price": _sec_cbp}
                else:
                    # wick-only reach of secondary: wait for 1m arm-confirm bar
                    position["active"]["cautious"] = "secondary_surpassed"
                    save_position(position)
                    return None

            if cautious_initial is not None and _surpassed(cautious_initial):
                _initial_dist = abs(cautious_initial - _fill_price) if _fill_price else float("inf")
                if _initial_dist < INITIAL_STOP_MIN_DIST_PTS:
                    return None  # initial level too close to entry — let original stop stand
                if _close_beyond(cautious_initial):
                    _ref5 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction, period_minutes=5)
                    position["active"]["cautious"] = "initial"
                    position["active"]["cautious_break_price"] = _floored_break_price(_ref5, direction, _fill_price, buffer_pts=BREAKEVEN_BUFFER_PTS)
                    position["conf_bar_exit"] = _ref_bar_to_dict(_ref5)
                    save_position(position)
                    if position["active"]["cautious_break_price"] is None:
                        return None  # deferred: trail will arm once price clears entry
                    return {"kind": "new-stop-exit", "time": now.isoformat(),
                            "price": position["active"]["cautious_break_price"], "level": "initial", "level_name": _lv1,
                            "cautious_break_price": position["active"]["cautious_break_price"]}
                else:
                    # wick-only: move stop to midpoint between original stop and initial target
                    position["active"]["cautious"] = "initial_surpassed"
                    _orig_stop = active.get("stop")
                    if _orig_stop is not None and cautious_initial is not None:
                        _mid_cbp = (float(_orig_stop) + cautious_initial) / 2.0
                        position["active"]["cautious_break_price"] = _mid_cbp
                    save_position(position)
                    _mid_cbp_val = position["active"].get("cautious_break_price")
                    if _mid_cbp_val is not None:
                        return {"kind": "new-stop-exit", "time": now.isoformat(),
                                "price": float(_mid_cbp_val), "level": "initial_mid", "level_name": _lv1,
                                "cautious_break_price": float(_mid_cbp_val)}
                    return None

            return None

        # ---- 3a2: initial surpassed — wait for 1m arm-confirm bar ----------
        if cautious_state == "initial_surpassed":
            # Break check for the midpoint stop placed on wick-touch.
            _break_price = active.get("cautious_break_price")
            if _break_price is not None:
                _broke = (bar_high > float(_break_price)) if direction == "down" \
                         else (bar_low  < float(_break_price))
                if _broke:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return {"kind": "stop-exit", "time": now.isoformat(),
                            "price": float(_break_price), "reason": "cautious-initial-break",
                            "close_reason": _cr1}

            # If secondary was reached this bar, upgrade immediately.
            if cautious_secondary is not None and _surpassed(cautious_secondary):
                if _close_beyond(cautious_secondary):
                    _ref1 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction)
                    position["active"]["cautious"] = "secondary"
                    _sec_cbp = _fill_price if _ath_secondary else _floored_break_price(_ref1, direction, _fill_price)
                    position["active"]["cautious_break_price"] = _sec_cbp
                    position["conf_bar_exit"] = {} if _ath_secondary else _ref_bar_to_dict(_ref1)
                    save_position(position)
                    if position["active"]["cautious_break_price"] is None:
                        return None
                    return {"kind": "new-stop-exit", "time": now.isoformat(),
                            "price": _sec_cbp, "level": "secondary", "level_name": _lv2,
                            "cautious_break_price": _sec_cbp}
                else:
                    position["active"]["cautious"] = "secondary_surpassed"
                    save_position(position)
                    return None

            if cautious_initial is not None:
                _opp_close = (bar_close < bar_open) if direction == "up" else (bar_close > bar_open)
                # Arm break-even on a confirmed close beyond the initial level OR on an
                # opposite-close pullback bar — both give a structural 5-min reference bar.
                if _close_beyond(cautious_initial) or _opp_close:
                    _ref5 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction, period_minutes=5)
                    _new_cbp = _floored_break_price(_ref5, direction, _fill_price, buffer_pts=BREAKEVEN_BUFFER_PTS)
                    _cur_cbp = active.get("cautious_break_price")
                    # Only tighten — never loosen the midpoint stop placed on wick-touch.
                    if _new_cbp is not None and (_cur_cbp is None or
                            (direction == "up"   and _new_cbp > float(_cur_cbp)) or
                            (direction == "down" and _new_cbp < float(_cur_cbp))):
                        position["active"]["cautious_break_price"] = _new_cbp
                    position["active"]["cautious"] = "initial"
                    position["conf_bar_exit"] = _ref_bar_to_dict(_ref5)
                    save_position(position)
                    if position["active"]["cautious_break_price"] is None:
                        return None  # deferred: trail will arm once price clears entry
                    _cbp = float(position["active"]["cautious_break_price"])
                    # If the arm-confirm bar itself breaches the break price, exit immediately.
                    _already_broke = (bar_high > _cbp) if direction == "down" else (bar_low < _cbp)
                    if _already_broke:
                        _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                        save_position(position)
                        save_hypothesis(hypothesis)
                        return {"kind": "stop-exit", "time": now.isoformat(),
                                "price": _cbp, "reason": "cautious-initial-break",
                                "close_reason": _cr1}
                    _kind = "move-stop-exit" if _cur_cbp is not None else "new-stop-exit"
                    return {"kind": _kind, "time": now.isoformat(),
                            "price": _cbp, "level": "initial", "level_name": _lv1,
                            "cautious_break_price": _cbp}

            return None

        # ---- 3b: initial cautious — break when price crosses snapshot body-high ----
        if cautious_state == "initial":
            # Upgrade to secondary if secondary level is now reached.
            if cautious_secondary is not None and _surpassed(cautious_secondary):
                if _close_beyond(cautious_secondary):
                    _ref1 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction)
                    _had_prior_stop = active.get("cautious_break_price") is not None
                    position["active"]["cautious"] = "secondary"
                    _sec_cbp = _fill_price if _ath_secondary else _floored_break_price(_ref1, direction, _fill_price)
                    position["active"]["cautious_break_price"] = _sec_cbp
                    position["conf_bar_exit"] = {} if _ath_secondary else _ref_bar_to_dict(_ref1)
                    save_position(position)
                    if position["active"]["cautious_break_price"] is None:
                        return None
                    return {"kind": "move-stop-exit" if _had_prior_stop else "new-stop-exit",
                            "time": now.isoformat(),
                            "price": _sec_cbp, "level": "secondary", "level_name": _lv2,
                            "cautious_break_price": _sec_cbp}
                else:
                    # wick-only reach of secondary: wait for 1m arm-confirm bar
                    position["active"]["cautious"] = "secondary_surpassed"
                    save_position(position)
                    return None

            # Trail the stop every 5m: if a newer completed 5m same-direction bar has a
            # tighter body bound than the stored break price, slide it and notify dispatch
            # to move the IB stop order.
            _trail_ref5 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction, period_minutes=5)
            _trail_price5 = _floored_break_price(_trail_ref5, direction, _fill_price, buffer_pts=BREAKEVEN_BUFFER_PTS)
            _trail_moved = False
            if _trail_price5 is not None:
                _cur_cbp = active.get("cautious_break_price")
                if _cur_cbp is not None:
                    _tighter = (direction == "up"   and _trail_price5 > float(_cur_cbp)) or \
                               (direction == "down" and _trail_price5 < float(_cur_cbp))
                    if _tighter:
                        active["cautious_break_price"] = _trail_price5
                        position["conf_bar_exit"] = _ref_bar_to_dict(_trail_ref5)
                        save_position(position)
                        _trail_moved = True

            # Break check uses the potentially-updated cautious_break_price.
            _break_price = active.get("cautious_break_price")
            if _break_price is not None:
                _broke = (bar_high > float(_break_price)) if direction == "down" \
                         else (bar_low  < float(_break_price))
                if _broke:
                    _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                    save_position(position)
                    save_hypothesis(hypothesis)
                    return {"kind": "stop-exit", "time": now.isoformat(),
                            "price": float(_break_price), "reason": "cautious-initial-break",
                            "close_reason": _cr1}

            if _trail_moved:
                return {"kind": "move-stop-exit", "time": now.isoformat(),
                        "price": bar_close, "cautious_break_price": active["cautious_break_price"],
                        "level": "initial", "level_name": _lv1}

            return None

        # ---- 3b2: secondary surpassed — wait for 1m arm-confirm bar --------
        if cautious_state == "secondary_surpassed":
            if cautious_secondary is not None:
                _opp_close = (bar_close < bar_open) if direction == "up" else (bar_close > bar_open)
                if (_opp_close and not _close_beyond(cautious_secondary)) or _close_beyond(cautious_secondary):
                    _ref1 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction)
                    _had_prior_stop = active.get("cautious_break_price") is not None
                    position["active"]["cautious"] = "secondary"
                    _sec_cbp = _fill_price if _ath_secondary else _floored_break_price(_ref1, direction, _fill_price)
                    position["active"]["cautious_break_price"] = _sec_cbp
                    position["conf_bar_exit"] = {} if _ath_secondary else _ref_bar_to_dict(_ref1)
                    save_position(position)
                    if position["active"]["cautious_break_price"] is None:
                        return None
                    return {"kind": "move-stop-exit" if _had_prior_stop else "new-stop-exit",
                            "time": now.isoformat(),
                            "price": _sec_cbp, "level": "secondary", "level_name": _lv2,
                            "cautious_break_price": _sec_cbp}
            return None

        # ---- 3c: secondary cautious — 20m bar-body for ATH levels, else snapshot body-high ----
        if cautious_state in ("secondary", "yes"):
            if _ath_secondary:
                # Break-even stop: IB stop sits at fill_price; exit only on full reversal to entry.
                _cbp = active.get("cautious_break_price")
                if _cbp is not None:
                    _be_broken = (bar_high > float(_cbp)) if direction == "down" \
                                 else (bar_low  < float(_cbp))
                    if _be_broken:
                        _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                        save_position(position)
                        save_hypothesis(hypothesis)
                        return {"kind": "stop-exit", "time": now.isoformat(),
                                "price": float(_cbp), "reason": "cautious-secondary-break",
                                "close_reason": _cr2}
                # ATH secondary: wait for 20m candle confirmation; no trailing.
                _conf_minutes = 20
                ts = pd.Timestamp(now)
                if ts.minute % _conf_minutes == 0:
                    conf_start = ts - pd.Timedelta(minutes=_conf_minutes)
                    conf_bars = mnq_1m_recent[mnq_1m_recent.index >= conf_start]
                    if not conf_bars.empty:
                        conf_open  = float(conf_bars["Open"].iloc[0])
                        conf_close = float(conf_bars["Close"].iloc[-1])
                        opposite_body = (conf_close < conf_open) if direction == "up" \
                                        else (conf_close > conf_open)
                        if opposite_body:
                            _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                            save_position(position)
                            save_hypothesis(hypothesis)
                            return _market_close_signal(now, bar_mid, reason=f"cautious-{_conf_minutes}m-break", close_reason=_cr2)
            else:
                # Trail the stop every 1m: if a newer completed 1m same-direction bar has a
                # tighter body bound than the stored break price, slide it and notify dispatch.
                _trail_ref1 = _last_same_dir_ref_bar(mnq_1m_recent, bar_time_str, direction)
                _trail_price1 = _floored_break_price(_trail_ref1, direction, _fill_price)
                _trail_moved = False
                if _trail_price1 is not None:
                    _cur_cbp = active.get("cautious_break_price")
                    if _cur_cbp is not None:
                        _tighter = (direction == "up"   and _trail_price1 > float(_cur_cbp)) or \
                                   (direction == "down" and _trail_price1 < float(_cur_cbp))
                        if _tighter:
                            active["cautious_break_price"] = _trail_price1
                            position["conf_bar_exit"] = _ref_bar_to_dict(_trail_ref1)
                            save_position(position)
                            _trail_moved = True

                # Secondary exits use bar *close* — intrabar wicks are ignored.
                _break_price = active.get("cautious_break_price")
                if _break_price is not None:
                    _broke = (bar_close > float(_break_price)) if direction == "down" \
                             else (bar_close < float(_break_price))
                    if _broke:
                        _clear_position_and_hypothesis(position, hypothesis, clear_active=True)
                        save_position(position)
                        save_hypothesis(hypothesis)
                        return {"kind": "stop-exit", "time": now.isoformat(),
                                "price": float(_break_price), "reason": "cautious-secondary-break",
                                "close_reason": _cr2}

                if _trail_moved:
                    return {"kind": "move-stop-exit", "time": now.isoformat(),
                            "price": bar_close, "cautious_break_price": active["cautious_break_price"],
                            "level": "secondary", "level_name": _lv2}

        return None

    # ------------------------------------------------------------------
    # Step 4: no open position — scan for opposite-direction liquidity break.
    # ------------------------------------------------------------------
    liquidities = daily.get("liquidities", [])

    # Daily-mid invalidation: if the hypothesized direction is contradicted by price
    # crossing the daily mid (e.g. direction=up but close fell below mid), the thesis
    # is stale — reset before placing any new entry. Skipped while the manual
    # direction lock is set (GIL-8). O1 (GIL-34): also suspended inside the open
    # window when the flag is ON, so a fresh hypothesis survives the open mayhem.
    if daily_mid_price is not None and _mid_cross_guard and not _manual_lock and not _suspend_daily_mid:
        _mid_broken = (direction == "up"   and bar_close < daily_mid_price) or \
                      (direction == "down" and bar_close > daily_mid_price)
        if _mid_broken:
            hypothesis["direction"] = "none"
            position["conf_bar_entry"] = {}
            position["conf_bar_exit"]  = {}
            position["stop_entry"] = ""
            position["session_mid_crosses"] = position.get("session_mid_crosses", 0) + 1
            save_position(position)
            save_hypothesis(hypothesis)
            return {
                "kind":             "trend-broken",
                "time":             now.isoformat(),
                "direction":        direction,
                "broken_direction": direction,
                "level_name":       "daily_mid",
                "level_price":      daily_mid_price,
                "price":            bar_close,
            }

    # Weekly-mid invalidation: same logic applied to the broader weekly range.
    # Skipped while the manual direction lock is set (GIL-8).
    if weekly_mid_price is not None and _weekly_mid_cross_guard and not _manual_lock:
        _wm_broken = (direction == "up"   and bar_close < weekly_mid_price) or \
                     (direction == "down" and bar_close > weekly_mid_price)
        if _wm_broken:
            hypothesis["direction"] = "none"
            position["conf_bar_entry"] = {}
            position["conf_bar_exit"]  = {}
            position["stop_entry"] = ""
            save_position(position)
            save_hypothesis(hypothesis)
            return {
                "kind":             "trend-broken",
                "time":             now.isoformat(),
                "direction":        direction,
                "broken_direction": direction,
                "level_name":       "weekly_mid",
                "level_price":      weekly_mid_price,
                "price":            bar_close,
            }

    # Session ATH straddle: bar crossed the fixed 09:20 ATH with direction not "down".
    # This is the first time into uncharted territory — invalidate the "up" thesis.
    if _session_ath_straddle:
        hypothesis["direction"] = "none"
        position["conf_bar_entry"] = {}
        position["conf_bar_exit"]  = {}
        position["stop_entry"] = ""
        save_position(position)
        save_hypothesis(hypothesis)
        return {
            "kind":          "ath-crossed",
            "time":          now.isoformat(),
            "price":         bar_close,
            "bar_high":      bar_high,
            "bar_low":       bar_low,
            "all_time_high": _session_ath,
        }

    # Dynamic ATH straddle: already in "down" territory above session_ath and the
    # running high was straddled. Hypothesis may need updating (cautious prices drift
    # as new highs form). No trend-broken — direction is already "down".
    if _dynamic_ath_straddle:
        return {
            "kind":          "dynamic-ath-crossed",
            "time":          now.isoformat(),
            "price":         bar_close,
            "bar_high":      bar_high,
            "bar_low":       bar_low,
            "all_time_high": _old_ath,
        }

    _HIGH_PRIO_LEVELS = {"week_high", "week_low", "day_high", "day_low"}

    # After trend-broken fires on a level, suppress re-fires on the same level+direction
    # for this many minutes.  Prevents a whipsaw around a level from repeatedly cancelling
    # pending limit entries before they can fill.
    _TREND_BROKEN_COOLDOWN_MINUTES = 10

    _now_ts = pd.Timestamp(now)
    _cooldowns = position.get("trend_broken_cooldowns", [])
    _active_cooldown_keys = {
        (c["level_name"], c["direction"])
        for c in _cooldowns
        if pd.Timestamp(c["expires_at"]) > _now_ts
    }

    for level in liquidities:
        if level.get("kind") != "level":
            continue
        level_name = level.get("name", "")
        if level_name not in _HIGH_PRIO_LEVELS:
            continue
        _in_cooldown = (level_name, direction) in _active_cooldown_keys

        level_price = float(level["price"])

        triggered = False
        extra: dict = {}
        if direction == "up":
            if bar_low <= level_price and bar_high >= level_price:
                triggered = True
                extra = {"bar_low": bar_low}
        else:  # direction == "down"
            if bar_high >= level_price and bar_low <= level_price:
                triggered = True
                extra = {"bar_high": bar_high}

        if triggered:
            if not _in_cooldown:
                expires_at = (_now_ts + pd.Timedelta(minutes=_TREND_BROKEN_COOLDOWN_MINUTES)).isoformat()
                new_cooldowns = [c for c in _cooldowns
                                 if not (c["level_name"] == level_name and c["direction"] == direction)]
                new_cooldowns.append({"level_name": level_name, "direction": direction, "expires_at": expires_at})
                position["trend_broken_cooldowns"] = new_cooldowns
                save_position(position)
            sig = {
                "kind":           "level-swept",
                "time":           now.isoformat(),
                "price":          bar_close,
                "direction":      direction,
                "level_name":     level_name,
                "level_price":    level_price,
                "cooldown_active": _in_cooldown,
            }
            sig.update(extra)
            return sig

    return None
