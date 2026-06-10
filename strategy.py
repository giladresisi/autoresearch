# strategy.py
# Per-5m-bar entry / stop / direction-mismatch logic for SMT v2 pipeline.
# Pure compute: reads hypothesis.json and position.json; updates position.json;
# returns an Optional[Signal] dict. No parquet loading.

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

import session_times
import smt_state

_DIR_UP            = "up"
_DIR_DOWN          = "down"
_CONF_BAR_MINS     = 5   # default confirmation bar window
_CONF_BAR_MINS_ATH = 15  # confirmation bar window when above session ATH
_STOP_WICK_CAP     = 15.0  # max pts a conf-bar wick can extend the stop beyond the body
MAX_FAILED_ENTRIES = 2   # block new entries once this many stops have been hit this hypothesis
_O5_FALLBACK_DIST  = 100.0  # O5: use prior window as pseudo-conf when entry range is this far behind price
MIN_HEADROOM_PTS   = 10.0   # min room from entry to nearest opposing level; entries with less are
                            # gated (reward:risk floor — see _headroom_ok; tune via backtest)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_last_bar(
    mnq_1m_recent: pd.DataFrame,
    now: datetime,
    bar_direction: str,
    minutes: int,
    not_before: str,
) -> Optional[dict]:
    """Return the most recently completed `minutes`-bar that moved in bar_direction.

    bar_direction='down' → bearish (close < open); 'up' → bullish (close > open).
    not_before: ISO timestamp — skip if last period started more than `minutes` before this.
    """
    if mnq_1m_recent is None or mnq_1m_recent.empty:
        return None

    delta                = pd.Timedelta(minutes=minutes)
    current_period_start = pd.Timestamp(now).floor(f"{minutes}min")
    last_period_start    = current_period_start - delta

    if not_before:
        if last_period_start < pd.Timestamp(not_before) - delta:
            return None

    _idx = mnq_1m_recent.index
    _sp  = _idx.searchsorted(last_period_start,    side="left")
    _ep  = _idx.searchsorted(current_period_start, side="left")
    window = mnq_1m_recent.iloc[_sp:_ep]
    if window.empty:
        return None

    o = float(window.iloc[0]["Open"])
    c = float(window.iloc[-1]["Close"])
    if bar_direction == _DIR_DOWN and c < o:
        return {
            "time":      last_period_start.isoformat(),
            "high":      float(window["High"].max()),
            "low":       float(window["Low"].min()),
            "body_high": max(o, c),
            "body_low":  min(o, c),
        }
    if bar_direction == _DIR_UP and c > o:
        return {
            "time":      last_period_start.isoformat(),
            "high":      float(window["High"].max()),
            "low":       float(window["Low"].min()),
            "body_high": max(o, c),
            "body_low":  min(o, c),
        }
    return None


def _bar_crosses(bar: dict, price: float) -> bool:
    """Return True if the bar's H/L range spans *price* (inclusive)."""
    return bar["low"] <= price <= bar["high"]


# Minimum fraction of the bar range that must be on the favourable side of the
# close before an entry is accepted.  A long entry on a bar that closed in the
# bottom 40 % of its range (shooting-star shape) is likely a wick-triggered
# false signal; the same logic applies symmetrically for shorts.
_CPR_MIN = 0.40

def _entry_bar_cpr_ok(bar: dict, direction: str) -> bool:
    """Return True if the entry bar's close position meets the quality threshold."""
    rng = bar["high"] - bar["low"]
    if rng < 0.25:
        return True  # near-doji — no meaningful signal either way
    if direction == _DIR_UP:
        return (bar["close"] - bar["low"]) / rng >= _CPR_MIN
    return (bar["high"] - bar["close"]) / rng >= _CPR_MIN


def _o5_fallback(
    hypothesis: dict,
    direction: str,
    mnq_bar: dict,
    mnq_1m_recent: pd.DataFrame,
    now: datetime,
    bar_mins: int,
) -> Optional[dict]:
    """O5: return the prior bar_mins-window as a pseudo-conf bar when the normal
    confirmation bar is absent AND all entry ranges are >_O5_FALLBACK_DIST pts
    behind the current price (unreachable without a major reversal).

    Guard: the current 1m bar must be moving in the hypothesis direction (or be a
    doji). If price is already reversing at the 1m level, the momentum has turned
    and O5 would be chasing against it.

    The existing MAX_CONFIRMATION_BODY_PTS check still applies to the returned
    window, so large momentum candles are still filtered out.
    """
    # Current bar must agree with hypothesis direction (dojis are neutral — allowed).
    bar_close = float(mnq_bar["close"])
    bar_open  = float(mnq_bar["open"])
    if direction == _DIR_UP   and bar_close < bar_open:
        return None
    if direction == _DIR_DOWN and bar_close > bar_open:
        return None

    entry_ranges = hypothesis.get("entry_ranges", [])
    if not entry_ranges:
        return None
    if direction == _DIR_UP:
        highs_behind = [r["high"] for r in entry_ranges if r.get("high", 0) < bar_close]
        if not highs_behind or (bar_close - max(highs_behind)) < _O5_FALLBACK_DIST:
            return None
    else:
        lows_behind = [r["low"] for r in entry_ranges if r.get("low", 99999) > bar_close]
        if not lows_behind or (min(lows_behind) - bar_close) < _O5_FALLBACK_DIST:
            return None
    if mnq_1m_recent is None or mnq_1m_recent.empty:
        return None
    delta        = pd.Timedelta(minutes=bar_mins)
    period_end   = pd.Timestamp(now).floor(f"{bar_mins}min")
    period_start = period_end - delta
    idx = mnq_1m_recent.index
    sp  = idx.searchsorted(period_start, side="left")
    ep  = idx.searchsorted(period_end,   side="left")
    window = mnq_1m_recent.iloc[sp:ep]
    if window.empty:
        return None
    o = float(window.iloc[0]["Open"])
    c = float(window.iloc[-1]["Close"])
    return {
        "time":      period_start.isoformat(),
        "high":      float(window["High"].max()),
        "low":       float(window["Low"].min()),
        "body_high": max(o, c),
        "body_low":  min(o, c),
    }


# ---------------------------------------------------------------------------
# Headroom / mid-zone gating helpers (R2/R3)
# ---------------------------------------------------------------------------

def _session_mids(liquidities: list) -> "tuple[float | None, float | None]":
    """Return (daily_mid, weekly_mid) from level liquidities, or None per axis if a bound
    is missing. Single source of truth for the mid derivation also used in Section 3."""
    _liq_map = {l["name"]: l["price"] for l in (liquidities or [])
                if l.get("kind") == "level"}
    _dh, _dl = _liq_map.get("day_high"),  _liq_map.get("day_low")
    _wh, _wl = _liq_map.get("week_high"), _liq_map.get("week_low")
    daily_mid  = (_dh + _dl) / 2.0 if _dh is not None and _dl is not None else None
    weekly_mid = (_wh + _wl) / 2.0 if _wh is not None and _wl is not None else None
    return daily_mid, weekly_mid


def _first_target_ahead(entry: float, direction: str, targets: list) -> "float | None":
    """Nearest hypothesis-target price strictly ahead of `entry` in `direction`."""
    ahead = []
    for t in (targets or []):
        price = t.get("price")
        if price is None:
            continue
        if direction == _DIR_UP and price > entry:
            ahead.append(price)
        elif direction == _DIR_DOWN and price < entry:
            ahead.append(price)
    if not ahead:
        return None
    # "Nearest ahead" = smallest for up (just above), largest for down (just below).
    return min(ahead) if direction == _DIR_UP else max(ahead)


def _nearest_opposing_level(entry: float, direction: str, daily_mid, weekly_mid,
                            targets: list) -> "float | None":
    """Nearest of {daily_mid, weekly_mid, first target ahead} that lies AHEAD of `entry`
    in `direction` (up: level > entry; down: level < entry). None if none ahead."""
    candidates = []
    for lvl in (daily_mid, weekly_mid, _first_target_ahead(entry, direction, targets)):
        if lvl is None:
            continue
        if direction == _DIR_UP and lvl > entry:
            candidates.append(lvl)
        elif direction == _DIR_DOWN and lvl < entry:
            candidates.append(lvl)
    if not candidates:
        return None
    return min(candidates, key=lambda lvl: abs(lvl - entry))


def _headroom_ok(entry: float, stop: float, direction: str, liquidities: list,
                 targets: list) -> bool:
    """True if the prospective entry has room to run (R3).

    headroom = distance from `entry` to the nearest opposing level ahead;
    risk     = abs(entry - stop).
    Passes when there is NO opposing level ahead (open road), else requires
    headroom >= max(risk, MIN_HEADROOM_PTS) — i.e. reward:risk >= ~1 with a fixed floor.
    """
    daily_mid, weekly_mid = _session_mids(liquidities)
    lvl = _nearest_opposing_level(entry, direction, daily_mid, weekly_mid, targets)
    if lvl is None:
        return True
    headroom = abs(lvl - entry)
    risk = abs(entry - stop)
    return headroom >= max(risk, MIN_HEADROOM_PTS)


def _make_signal(kind: str, now: datetime, price: float, **kwargs) -> dict:
    """Build a JSON-serialisable signal dict."""
    sig: dict = {
        "kind":  kind,
        "time":  now.isoformat(),
        "price": float(price),
    }
    sig.update(kwargs)
    return sig


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_strategy(
    now: datetime,
    mnq_bar: dict,
    mnq_1m_recent: pd.DataFrame,
    fill_check_only: bool = False,
    prefer_market_entry: bool = False,
) -> Optional[dict]:
    """Process a completed 1m bar and return an Optional Signal.

    Args:
        now:           Timestamp of the bar boundary.
        mnq_bar:       Dict with keys time, open, high, low, close, body_high, body_low.
        mnq_1m_recent: Recent 1m bars (for context; not currently used in strategy logic).

    Returns:
        A Signal dict or None.
    """
    hypothesis = smt_state.load_hypothesis()
    position   = smt_state.load_position()

    direction    = hypothesis.get("direction", "none")
    formed_at    = hypothesis.get("formed_at", "")

    # ------------------------------------------------------------------ #
    # Section 2: No active position                                        #
    # ------------------------------------------------------------------ #
    if not position["active"]:
        # Cancel any unfilled stop entry at the first bar after an entry window closes.
        _prev_bar_time = (now - timedelta(minutes=5)).time()
        if (
            not session_times.is_entry_allowed(now.time())
            and session_times.is_entry_allowed(_prev_bar_time)
            and position["stop_entry"] != ""
        ):
            _cancel_price = float(position["stop_entry"])
            position["stop_entry"]     = ""
            position["stop_direction"] = ""
            position["conf_bar_entry"] = {}
            smt_state.save_position(position)
            return _make_signal("cancel-stop-entry", now, _cancel_price, reason="entry-window-closed")

        # confidence=high: global conviction active — no automatic entries (limit or market).
        _global = smt_state.load_global()
        if _global.get("confidence") == "high":
            return None

        _session_ath = _global.get("all_time_high") or _global.get("session_ath")
        _above_session_ath = (
            _session_ath is not None and float(mnq_bar["high"]) >= float(_session_ath)
        )
        if direction == _DIR_UP and _above_session_ath:
            return None

        # 2.1 Early-exit conditions — cancel any pending limit if direction changed.
        stop_entry = position["stop_entry"]
        stop_direction = position.get("stop_direction", "")
        if stop_entry != "" and stop_direction != "" and (
            direction == "none" or direction != stop_direction
        ):
            _cancel_price = float(stop_entry)
            position["stop_entry"]      = ""
            position["stop_direction"]  = ""
            position["conf_bar_entry"] = {}
            smt_state.save_position(position)
            _reason = "direction-none" if direction == "none" else "direction-changed"
            return _make_signal("cancel-stop-entry", now, _cancel_price, reason=_reason)

        if direction == "none":
            return None
        if position["failed_entries"] > MAX_FAILED_ENTRIES:
            return None

        _daily = smt_state.load_daily()
        _chop_rng = _daily.get("overnight_range", 0)
        _chop_mid_x = position.get("session_mid_crosses", 0)
        if (
            _chop_rng > 0
            and _chop_rng < 150.0
            and _chop_mid_x >= 4
            and position["failed_entries"] >= MAX_FAILED_ENTRIES
        ):
            return None

        _MARKET_ENTRY_THRESHOLD = 0.0  # only market-enter when bar opened past the entry level (gapped through)
        MIN_STOP_DISTANCE = 5.0
        # Tradovate rejects a stop order whose trigger price is within ~1 tick of the market price
        # because the order fills immediately as a market order and is treated as invalid.
        # Enforce a 10-pt buffer: if the natural entry level is too close to bar open, push it
        # further away in the intended direction so the order reaches the exchange with room to spare.
        MIN_APPROACH_PTS = 10.0
        MAX_CONFIRMATION_BODY_PTS = 25.0  # reject momentum/reversal bars as confirmation
        # STP->MKT downgrade safety (see feature.md). After R1 the downgrade keys off the
        # TRIGGER itself, not a proximity band: a stop market-fills only once bar_mid has
        # reached/passed entry_price. STP_MKT_PROXIMITY_PTS is retained as the documented
        # live<->backtest contract anchor (kept in sync with execution/pickmytrade.py) but no
        # longer slackens the will_market_fill condition below.
        STP_MKT_PROXIMITY_PTS      = 5.0    # legacy proximity band — R1 removed its use in will_market_fill
        MKT_FILL_MIN_STOP_DISTANCE = 10.0   # min stop distance from the EXPECTED market fill (tune via backtest)
        MAX_ENTRY_CHASE_PTS        = 10.0   # skip entry if market already ran this far past it (tune via backtest)

        # 2.4 Fill check runs FIRST so a limit that fills on the same bar as a new
        # confirmation bar is detected rather than overwritten by a move-limit signal.
        # Long fills when bar_high >= limit; short fills when bar_low <= limit.
        stop_entry = position["stop_entry"]
        _entry_f = float(stop_entry) if stop_entry != "" else None
        _entry_reached = _entry_f is not None and (
            (direction == _DIR_UP   and float(mnq_bar["high"]) >= _entry_f) or
            (direction == _DIR_DOWN and float(mnq_bar["low"])  <= _entry_f)
        )
        if stop_entry != "" and _entry_reached and not position.get("stop_entry_unplaced"):
            fill_price   = float(stop_entry)
            pending_stop = position.get("pending_stop")
            if pending_stop is not None:
                stop = float(pending_stop)
            else:
                conf_bar = position["conf_bar_entry"]
                if conf_bar:
                    # Backward-compat: positions written before pending_stop was added
                    if direction == _DIR_UP:
                        stop = max(float(conf_bar["low"]), float(conf_bar["body_low"]) - _STOP_WICK_CAP)
                    else:
                        stop = min(float(conf_bar["high"]), float(conf_bar["body_high"]) + _STOP_WICK_CAP)
                else:
                    # Manual path: stop entry placed via trade.py — use bar_state.json
                    from smt_state import load_bar_state
                    bar_state = load_bar_state()
                    if bar_state is None:
                        print("[STRATEGY] fill detected but no bar_state.json — skipping fill", flush=True)
                        return None
                    stop = bar_state.get("potential_stop_long" if direction == _DIR_UP else "potential_stop_short")
                    if stop is None:
                        print("[STRATEGY] fill detected but potential_stop is null in bar_state — skipping fill", flush=True)
                        return None
                    stop = float(stop)
            # Fill as soon as price reaches the limit — bar close quality is irrelevant
            # for fill confirmation (the broker fills a limit order the instant price
            # touches it, regardless of where the bar closes).
            if abs(fill_price - float(stop)) >= MIN_STOP_DISTANCE:
                position["active"] = {
                    "time":       mnq_bar["time"],
                    "fill_price": fill_price,
                    "direction":  direction,
                    "stop":       stop,
                    "contracts":  2,
                    "cautious":   "no",
                    # Carry the manual/strategy tag from the stop entry through the fill
                    # so manual positions stay exempt from the direction-mismatch close.
                    "source":     position.get("stop_entry_source", "strategy"),
                }
                position["stop_entry"]      = ""
                position["stop_direction"]  = ""
                position.pop("stop_entry_source", None)
                # conf_bar_entry intentionally preserved across fill so that after a
                # stop-out the same 5m bar cannot be reused as confirmation for re-entry.
                smt_state.save_position(position)
                # Addendum 4: re-anchor the cautious ladder to the ACTUAL fill price so
                # protection targets stay relevant when the fill lands far from formation.
                import hypothesis as _hyp_mod
                _hyp_mod.recompute_cautious_for_fill(
                    hypothesis, fill_price,
                    _daily.get("liquidities", []), _global.get("all_time_high"),
                    position.get("cautious_dist_shrinks", 0))
                smt_state.save_hypothesis(hypothesis)
                # SMT-v2 Phase 1: freeze the fill-anchored (post-recompute) management
                # direction + cautious ladder into active — immutable for the life of the
                # trade. trend.py Step-3 manages off this snapshot, not the live hypothesis.
                smt_state.freeze_active_mgmt(position["active"], direction, hypothesis)
                smt_state.save_position(position)
                return _make_signal("stop-entry-filled", now, fill_price, direction=direction, stop=stop)

        if fill_check_only:
            return None

        # 2.3 Find the most recent completed opposite bar and set/update limit.
        # Above session ATH, require a 15m confirmation bar (more restrictive).
        # Only emits a signal when the reference bar changes.
        _opp_dir  = _DIR_UP if direction == _DIR_DOWN else _DIR_DOWN
        _bar_mins = _CONF_BAR_MINS_ATH if _above_session_ath else _CONF_BAR_MINS
        opp_5m = _find_last_bar(mnq_1m_recent, now, _opp_dir, _bar_mins, formed_at)
        # _conf_is_o5: the confirmation bar came from the same-bar o5 pseudo-conf fallback
        # (not a real completed opposite 5m bar). Drives the R2 gate and conf attribution.
        _conf_is_o5 = False
        if opp_5m is None:
            opp_5m = _o5_fallback(hypothesis, direction, mnq_bar, mnq_1m_recent, now, _bar_mins)
            _conf_is_o5 = opp_5m is not None
        # Shared inputs for the headroom gate (R2/R3) and confirmation-path attribution.
        _liq      = _daily.get("liquidities", [])
        _targets  = hypothesis.get("targets", [])
        _conf_tag = "o5" if _conf_is_o5 else "normal"

        def _gated(price: float, reason: str) -> dict:
            """Emit a side-effect-free entry-gated signal (no position mutation).
            Unknown kind to live_orders.dispatch_order -> log-only, never a broker order."""
            return _make_signal("entry-gated", now, price,
                                direction=direction, gated=reason, conf=_conf_tag)

        if opp_5m is not None and (opp_5m["body_high"] - opp_5m["body_low"]) <= MAX_CONFIRMATION_BODY_PTS:
            body_end_price = opp_5m["body_high"] if direction == _DIR_UP else opp_5m["body_low"]
            current_conf_time = position.get("conf_bar_entry", {}).get("time", "")
            if opp_5m["time"] != current_conf_time or prefer_market_entry:
                conf_bar_snap = {
                    "time":      opp_5m["time"],
                    "high":      opp_5m["high"],
                    "low":       opp_5m["low"],
                    "body_high": opp_5m["body_high"],
                    "body_low":  opp_5m["body_low"],
                }

                bar_open = float(mnq_bar["open"])
                if direction == _DIR_UP:
                    approach = body_end_price - bar_open
                else:
                    approach = bar_open - body_end_price

                if approach < _MARKET_ENTRY_THRESHOLD or prefer_market_entry:
                    bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                    if direction == _DIR_UP:
                        stop = max(float(opp_5m["low"]), float(opp_5m["body_low"]) - _STOP_WICK_CAP)
                    else:
                        stop = min(float(opp_5m["high"]), float(opp_5m["body_high"]) + _STOP_WICK_CAP)
                    # Directional stop check: stop must be on the protective side of entry.
                    # abs() alone passes when the bar moved past the conf bar during the candle.
                    # On prefer_market_entry re-entries (post stop-exit sweep), push the stop
                    # just far enough to satisfy MIN_STOP_DISTANCE rather than rejecting.
                    if direction == _DIR_UP   and (bar_mid - float(stop)) < MIN_STOP_DISTANCE:
                        if prefer_market_entry:
                            stop = bar_mid - MIN_STOP_DISTANCE
                        else:
                            return None
                    if direction == _DIR_DOWN and (float(stop) - bar_mid) < MIN_STOP_DISTANCE:
                        if prefer_market_entry:
                            stop = bar_mid + MIN_STOP_DISTANCE
                        else:
                            return None
                    if not _entry_bar_cpr_ok(mnq_bar, direction):
                        return None
                    # R2 headroom gate (o5-only): reject same-bar o5 pseudo-conf market entries
                    # with no room to run to the nearest opposing level, so the pseudo-conf never
                    # becomes a persisted conf_bar. Scoped to o5 only — backtest showed a general
                    # headroom gate over-rejects legitimate breakouts that run through a mid.
                    if _conf_is_o5 and not _headroom_ok(bar_mid, stop, direction, _liq, _targets):
                        return _gated(bar_mid, "r2-o5-no-headroom")
                    position["active"] = {
                        "time":       mnq_bar["time"],
                        "fill_price": bar_mid,
                        "direction":  direction,
                        "stop":       stop,
                        "contracts":  2,
                        "cautious":   "no",
                    }
                    position["conf_bar_entry"] = conf_bar_snap
                    position["stop_entry"]      = ""
                    position["stop_direction"]  = ""
                    smt_state.save_position(position)
                    # Addendum 4: re-anchor the cautious ladder to the ACTUAL fill price.
                    import hypothesis as _hyp_mod
                    _hyp_mod.recompute_cautious_for_fill(
                        hypothesis, bar_mid,
                        _daily.get("liquidities", []), _global.get("all_time_high"),
                        position.get("cautious_dist_shrinks", 0))
                    smt_state.save_hypothesis(hypothesis)
                    # SMT-v2 Phase 1: freeze the fill-anchored (post-recompute) management
                    # direction + cautious ladder into active — immutable for the life of
                    # the trade. trend.py Step-3 manages off this snapshot.
                    smt_state.freeze_active_mgmt(position["active"], direction, hypothesis)
                    smt_state.save_position(position)
                    return _make_signal("market-entry", now, bar_mid, direction=direction,
                                        stop=stop, conf=_conf_tag)

                # Push entry away from current price if the natural level is too close.
                if direction == _DIR_UP:
                    entry_price = max(body_end_price, bar_open + MIN_APPROACH_PTS)
                else:
                    entry_price = min(body_end_price, bar_open - MIN_APPROACH_PTS)
                if direction == _DIR_UP:
                    stop_loss = max(float(opp_5m["low"]), float(opp_5m["body_low"]) - _STOP_WICK_CAP)
                else:
                    stop_loss = min(float(opp_5m["high"]), float(opp_5m["body_high"]) + _STOP_WICK_CAP)
                # Fix 1: if the executor will market-fill this (bar_mid has reached/passed the
                # trigger — R1), treat bar_mid as the expected fill and re-anchor the protective
                # stop to it using the trade's intended risk, so the stop survives the STP->MKT
                # downgrade. Record entry_price = expected fill so position.json matches the market
                # fill and the checks below measure the stop distance from the fill.
                bar_mid = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                # Fix 3: don't chase -- if the market has already run past the intended
                # entry on the trigger side by more than MAX_ENTRY_CHASE_PTS, a market fill
                # would land far worse than planned and the setup has already moved on.
                if direction == _DIR_UP   and bar_mid > entry_price + MAX_ENTRY_CHASE_PTS:
                    return None
                if direction == _DIR_DOWN and bar_mid < entry_price - MAX_ENTRY_CHASE_PTS:
                    return None
                # R1: mirror the live STP->MKT downgrade — the executor market-fills the moment
                # the market reaches the trigger (pickmytrade.place_entry); below the trigger the
                # stop rests legally as a resting stop_entry. The trigger is "reached" the instant
                # the price TOUCHES it intrabar — i.e. the bar EXTREME toward it (high for longs,
                # low for shorts), matching the resting-stop fill check above (bar_high>=entry) and
                # the live executor's bar-extreme downgrade. bar_mid lagged within the bar, so a
                # stop the market had already touched rested and was rejected by Tradovate
                # (D1/O2, 2026-06-11 — four rejected brackets, booked as phantom fills).
                will_market_fill = (
                    (direction == _DIR_UP   and float(mnq_bar["high"]) >= entry_price) or
                    (direction == _DIR_DOWN and float(mnq_bar["low"])  <= entry_price)
                )
                # A stop entry ultimately fills AT its trigger as a market/STP order — whether the
                # executor downgrades it to MKT now (will_market_fill) or it triggers on a later
                # tick. So the protective stop must clear MKT_FILL_MIN_STOP_DISTANCE from the
                # (expected) fill on BOTH branches: re-anchor to the expected fill (bar_mid when
                # the trigger is already reached, else the trigger itself) and floor the risk.
                # A conf-bar stop sitting a few pts under the trigger otherwise leaves almost no
                # room and stops out on the entry bar (incident 2026-06-04: resting stop entries
                # placed 5-6 pts off the conf low, instant stop-out). Mirrors the market path's
                # floor; max() only ever widens — a far conf-bar stop is left untouched.
                # Never BETTER than the trigger: a stop fires AT the trigger, so a touch fills
                # ~there; only a full gap-through (bar_mid already past the trigger) fills worse.
                if will_market_fill:
                    expected_fill = max(entry_price, bar_mid) if direction == _DIR_UP \
                        else min(entry_price, bar_mid)
                else:
                    expected_fill = entry_price
                risk = max(abs(stop_loss - entry_price), MKT_FILL_MIN_STOP_DISTANCE)
                if direction == _DIR_UP:
                    stop_loss = expected_fill - risk
                else:
                    stop_loss = expected_fill + risk
                entry_price = expected_fill
                # R2 headroom gate (o5-only, stop-entry path): reject before the o5 pseudo-conf
                # is persisted as conf_bar. Measured from the resting/expected entry to its stop.
                if _conf_is_o5 and not _headroom_ok(entry_price, stop_loss, direction, _liq, _targets):
                    return _gated(entry_price, "r2-o5-no-headroom")
                position["conf_bar_entry"] = conf_bar_snap
                kind = "new-stop-entry" if position["stop_entry"] == "" else "move-stop-entry"
                position["stop_entry"]     = entry_price
                position["stop_direction"] = direction
                position["pending_stop"]   = stop_loss
                smt_state.save_position(position)
                return _make_signal(kind, now, entry_price, stop=stop_loss, conf=_conf_tag)

        # Nothing triggered
        return None

    # ------------------------------------------------------------------ #
    # Section 3: Active position                                           #
    # ------------------------------------------------------------------ #
    active = position["active"]

    # 3.1 Direction mismatch (includes direction == "none")
    # Normalise position vocabulary (long/short) to hypothesis vocabulary (up/down).
    _pos_dir = active.get("direction", "")
    _pos_hyp_dir = "up" if _pos_dir == "long" else ("down" if _pos_dir == "short" else _pos_dir)
    if direction == "none" or direction != _pos_hyp_dir:
        # SMT-v2 Phase 1 (.agents/plans/smt-v2-decouple-active-position.md): the automatic
        # direction-mismatch market-close is REMOVED. An open position is now managed off the
        # frozen snapshot (active["mgmt_direction"] + frozen cautious ladder) by trend.py
        # Step-3, with cautious targets — not a force-close — deciding the exit. So a flipped
        # or none live hypothesis no longer flattens a live trade. strategy.py Section 3 does
        # nothing under a mismatch (the manual exemption is now redundant but kept explicit):
        # management is trend.py's responsibility, and running 3.2/3.3 here against a mismatched
        # live direction would double-manage. Return None.
        return None

    # 3.2 Stop crossed
    stop = active["stop"]
    active_dir = _pos_hyp_dir  # already normalised to up/down above
    stopped = False
    if active_dir == _DIR_UP and mnq_bar["low"] <= stop:
        stopped = True
    elif active_dir == _DIR_DOWN and mnq_bar["high"] >= stop:
        stopped = True

    if stopped:
        exit_price = stop
        position["active"]            = {}
        position["stop_entry"]       = ""
        position["conf_bar_exit"]    = {}
        # conf_bar_entry intentionally preserved: prevents immediate re-entry on the
        # same bar before the next 5m hypothesis re-evaluation can run.
        position["failed_entries"]    = position.get("failed_entries", 0) + 1
        position["cautious_dist_shrinks"] = position.get("cautious_dist_shrinks", 0) + 1

        # Flag for re-evaluation when the stop crossed the daily or weekly mid —
        # structural signals that the directional thesis has genuinely inverted.
        # Stops on the same side of both mids are noise and skip the re-run.
        _daily = smt_state.load_daily()
        _daily_mid, _weekly_mid = _session_mids(_daily.get("liquidities", []))
        _stop_crossed_daily = _daily_mid is not None and (
            (active_dir == _DIR_UP   and float(exit_price) < _daily_mid) or
            (active_dir == _DIR_DOWN and float(exit_price) > _daily_mid)
        )
        _stop_crossed_weekly = _weekly_mid is not None and (
            (active_dir == _DIR_UP   and float(exit_price) < _weekly_mid) or
            (active_dir == _DIR_DOWN and float(exit_price) > _weekly_mid)
        )
        if _stop_crossed_daily or _stop_crossed_weekly:
            position["reeval_after_stop"] = True

        smt_state.save_position(position)
        return _make_signal("stopped-out", now, exit_price)

    # 3.3 Above-ATH close: short position still above session ATH exits when a 15m
    # down bar (that stayed above ATH) forms and a 1m up bar breaks above its body_high.
    # Uses bar["low"] for both checks: if price drops below ATH on any bar the rule is off;
    # likewise the reference 15m bar must not have dipped below ATH during its period.
    if active_dir == _DIR_DOWN:
        _global = smt_state.load_global()
        _session_ath = _global.get("all_time_high") or _global.get("session_ath")
        if _session_ath is not None and float(mnq_bar["low"]) >= float(_session_ath):
            _last_15m_down = _find_last_bar(mnq_1m_recent, now, _DIR_DOWN, _CONF_BAR_MINS_ATH, active.get("time", ""))
            if (
                _last_15m_down is not None
                and float(_last_15m_down["low"]) >= float(_session_ath)
                and float(mnq_bar["close"]) > float(mnq_bar["open"])
                and float(mnq_bar["high"]) > float(_last_15m_down["body_high"])
            ):
                position["active"]           = {}
                position["stop_entry"]      = ""
                position["conf_bar_entry"]  = {}
                position["conf_bar_exit"]   = {}
                smt_state.save_position(position)
                _close_price = (float(mnq_bar["high"]) + float(mnq_bar["low"])) / 2.0
                return _make_signal(
                    "market-close", now, _close_price,
                    reason="above-ath-15m-reversal",
                    close_reason="above-ath-reversal",
                )

    # 3.4 Position active, no event
    return None


# ---------------------------------------------------------------------------
# Position reset helpers — called by daily.py and hypothesis.py so that all
# position.json writes go through the strategy module, not around it.
# ---------------------------------------------------------------------------

def reset_position_for_session() -> None:
    """Clear all active-trade and pending-limit fields at session open.

    Called by daily.run_daily once per session (09:20 ET).
    """
    pos = smt_state.load_position()
    pos["active"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["conf_bar_entry"] = {}
    pos["conf_bar_exit"]  = {}
    pos["failed_entries"] = 0
    pos["cautious_dist_shrinks"] = 0
    smt_state.save_position(pos)


def reset_position_for_new_hypothesis() -> None:
    """Clear entry-state fields on a none→up/down hypothesis transition.

    Called by hypothesis.run_hypothesis when a directional bias is established.
    Leaves 'active' untouched: a filled trade persists across hypothesis changes.
    """
    pos = smt_state.load_position()
    pos["failed_entries"] = 0
    pos["cautious_dist_shrinks"] = 0
    pos["conf_bar_entry"] = {}
    pos["conf_bar_exit"]  = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    smt_state.save_position(pos)
