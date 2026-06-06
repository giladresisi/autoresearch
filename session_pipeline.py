# session_pipeline.py
# Shared per-session bar-dispatch pipeline used by backtest_smt, signal_smt, automation.
# Fixes 8 live/backtest behavioral divergences by implementing the correct behavior once.
from __future__ import annotations

import copy
import datetime
import os
from typing import Callable

import pandas as pd

from zoneinfo import ZoneInfo

import paths
import daily as _daily_mod
import hypothesis as _hyp_mod
import smt_state as _smt_state
import strategy as _strat_mod
import trend as _trend_mod
from session_times import is_entry_allowed, cme_session_start as _cme_session_start, cme_session_date
from smt_state import save_bar_state

_ET = ZoneInfo("America/New_York")


def _mmax(a: "float | None", b: "float | None") -> "float | None":
    """max of two optional floats (None acts as 'no value')."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _mmin(a: "float | None", b: "float | None") -> "float | None":
    """min of two optional floats (None acts as 'no value')."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


class SessionPipeline:
    """Dispatches daily → trend → hypothesis → strategy for one trading session.

    Fixes: ATH seeding, hist_1hr/4hr to hypothesis, run_strategy every 1m bar,
    bar_dict body fields, consistent hourly resample, all-day 'recent' scope.
    """

    _STOP_WICK_CAP = 15.0

    def __init__(
        self,
        hist_mnq_1m: pd.DataFrame,
        hist_mes_1m: pd.DataFrame,
        emit_fn: Callable[[dict], None],
    ) -> None:
        self._hist_mnq_1m = hist_mnq_1m
        self._hist_mes_1m = hist_mes_1m
        self._emit = emit_fn
        self._daily_triggered = False
        self._hist_1hr: pd.DataFrame | None = None
        self._hist_4hr: pd.DataFrame | None = None
        # Rolling TF frames for the FVG liquidity scan: seeded at daily/startup from
        # hist + today's COMPLETED bars, then extended intra-session with each
        # just-completed 1hr/4hr bar resampled from live 1m (_extend_fvg_frames).
        # Kept separate from _hist_1hr/_hist_4hr, which the hypothesis paths consume
        # as strictly-pre-session history (they concat today's resample themselves).
        self._fvg_1hr: pd.DataFrame | None = None
        self._fvg_4hr: pd.DataFrame | None = None
        # Fast-path cache: last completed TF boundary already processed per frame.
        self._fvg_done_1hr: pd.Timestamp | None = None
        self._fvg_done_4hr: pd.Timestamp | None = None
        self._session_ath: float | None = None
        # Tracks (cautious_price_initial, cautious_price_secondary) of last emitted
        # new-hypothesis. Used to suppress redundant signals above session_ath.
        self._last_hyp_cautious: tuple[str, str] = ("", "")
        self._last_5m_processed: pd.Timestamp | None = None
        self._last_trend_hyp_1m: pd.Timestamp | None = None
        # Earliest bar timestamp at which force-eval fires, or None if not armed.
        # Both stop-exit and market-close come from trend.py (trend_sig), not strategy.py.
        # market-close → floor("1min")        fires immediately (next bar)
        # stop-exit    → floor("1min") + 1min wait one minute (cascade guard)
        # stopped-out  → floor("1min") + 1min wait one minute (cascade guard)
        # The minute gate prevents the 1s cascade (re-fill + re-stop within one second).
        self._force_entry_eval_after: pd.Timestamp | None = None
        # When True, the next force-eval will prefer a market-entry over a stop-entry.
        # Set after stop-exit (shallow sweep) — enter at bar_mid immediately.
        # Not set after market-close or stopped-out (let normal stop/approach logic apply).
        self._force_market_entry: bool = False
        # Bar close price when the current hypothesis was formed. Used to suppress
        # cooldown-path trend-broken when the level was already past at formation
        # (e.g. a 1s bar revisits a level the hypothesis already priced in).
        # An aggression override fires trend-broken regardless when the current bar is
        # more than _COOLDOWN_AGGRESSION_PTS beyond the formation offset from the level.
        self._hyp_formation_price: float | None = None
        # Set of (level_name, direction) pairs absorbed without direction change in the
        # non-cooldown level-swept path. Suppresses cooldown sweeps of the same level
        # even when _hyp_formation_price wouldn't (belt-and-suspenders).
        self._accepted_level_sweeps: set[tuple[str, str]] = set()
        self._COOLDOWN_AGGRESSION_PTS = 15.0
        # Tracks level names (from all liquidity "level" entries) swept at least once since
        # the last hypothesis direction change.  When a *new* level is crossed for the first
        # time under the current hypothesis and failed_entries is at the limit, it is
        # decremented by 1 to allow one additional re-entry attempt.
        self._swept_levels_since_hyp: set[str] = set()
        # Cached (name, price) pairs for every "level"-kind liquidity, populated at session
        # open and used for the per-bar sweep check below.
        self._ext_levels: list[tuple[str, float]] = []
        self._last_daily_date: "datetime.date | None" = None
        self._last_daily_minute: "pd.Timestamp | None" = None
        # Tracks whether the previous bar was inside an entry-allowed window.
        # Used to detect window-entry events and clear ghost positions.
        self._was_in_window: "bool | None" = None
        # bar_state cache: the [prev_5m, current_5m) window is constant within a 5m
        # block, so the potential-stop values only change once per block. Cache them
        # keyed by current_5m and rewrite only the per-bar "time" field.
        self._bar_state_5m: pd.Timestamp | None = None
        self._bar_state_vals: "tuple[float | None, float | None]" = (None, None)
        # Dynamic-liquidity hist tail: _hist_mnq_1m is constant per session and strictly
        # before today_mnq. _update_dynamic_liquidities only ever looks back ~1 week, so we
        # cache a small (8-day) tail once and concat that with today_mnq each bar instead of
        # re-concatenating + re-sorting the full 60-day hist on every one of ~80k bars.
        self._dyn_hist_tail: "pd.DataFrame | None" = None
        # Day/week H/L are running max/min over windows whose start is fixed for the whole
        # session: the constant hist contribution [window_start, session_start) is computed
        # once (keyed by window start) and combined with today_mnq's small max/min each bar,
        # avoiding a per-bar boolean mask over the ~11k-row combined frame.
        self._dyn_day_key: "pd.Timestamp | None" = None
        self._dyn_day_hl: "tuple[float | None, float | None]" = (None, None)
        self._dyn_week_key: "pd.Timestamp | None" = None
        self._dyn_week_hl: "tuple[float | None, float | None]" = (None, None)
        # Constant 18:00–session-open hist sliver (the ~5 pre-18:05 bars the asia session
        # window needs); concatenated with today_mnq to feed _session_bars a small frame.
        self._dyn_sliver18: "pd.DataFrame | None" = None

    def on_daily_or_startup(self, now: pd.Timestamp, today_mnq: pd.DataFrame) -> None:
        """Compute fixed reference liquidities and seed ATH. Called on startup and at 09:20 ET daily."""
        from smt_state import (
            DEFAULT_DAILY, DEFAULT_GLOBAL,
            load_daily, load_global, save_daily, save_global,
        )
        from hypothesis import compute_live_hl_mid
        from daily import _session_bars

        # Cross-session ATH continuity: per-session global.json starts fresh, so carry the
        # dynamic all_time_high forward from the prior session before seeding from hist.
        # No-op in backtest (in-memory) mode — keeps backtests deterministic.
        _smt_state.seed_global_from_prior()

        # Seed all_time_high and session_ath from historical bars.
        _global = load_global()
        if not self._hist_mnq_1m.empty:
            _hist_ath = float(self._hist_mnq_1m["High"].max())
            _global["all_time_high"] = max(_global.get("all_time_high", 0.0), _hist_ath)
            _global["session_ath"] = _hist_ath
            self._session_ath = _hist_ath
        else:
            self._session_ath = None
        save_global(_global)

        # Fix #5: Unified hourly resample — 14-day window, label="left", no Volume.
        _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        _14d_ago = now - pd.Timedelta(days=14)
        if not self._hist_mnq_1m.empty:
            _1hr_full = (
                self._hist_mnq_1m.resample("1h", label="left")
                .agg(_agg)
                .dropna(subset=["Open"])
            )
            self._hist_1hr = _1hr_full[_1hr_full.index >= _14d_ago]
            _4hr_full = (
                self._hist_mnq_1m.resample("4h", label="left")
                .agg(_agg)
                .dropna(subset=["Open"])
            )
            self._hist_4hr = _4hr_full[_4hr_full.index >= _14d_ago]
        else:
            self._hist_1hr = pd.DataFrame(columns=list(_agg))
            self._hist_4hr = pd.DataFrame(columns=list(_agg))

        # Combine hist + today before run_daily_fixed so the midnight bar (00:00 ET)
        # is available when this fires at the London session start trigger.
        _combined = pd.concat([self._hist_mnq_1m, today_mnq]).sort_index()
        _combined = _combined[~_combined.index.duplicated(keep="last")]

        # Rolling FVG frames: hist + today's COMPLETED TF bars, 14d-bounded. A
        # still-forming bar is excluded — its H/L is not final, so it must not
        # complete an FVG yet. At first startup this equals the hist-only frames
        # (today_mnq is empty/just opened); at the 09:20 daily reset it keeps the
        # session's completed bars, so run_daily_fixed re-detects FVGs that formed
        # live earlier in the session instead of silently dropping them.
        if not _combined.empty:
            _fvg_1hr_full = (
                _combined.resample("1h", label="left").agg(_agg).dropna(subset=["Open"])
            )
            self._fvg_1hr = _fvg_1hr_full[
                (_fvg_1hr_full.index >= _14d_ago) & (_fvg_1hr_full.index < now.floor("1h"))
            ]
            _fvg_4hr_full = (
                _combined.resample("4h", label="left").agg(_agg).dropna(subset=["Open"])
            )
            self._fvg_4hr = _fvg_4hr_full[
                (_fvg_4hr_full.index >= _14d_ago) & (_fvg_4hr_full.index < now.floor("4h"))
            ]
        else:
            self._fvg_1hr = pd.DataFrame(columns=list(_agg))
            self._fvg_4hr = pd.DataFrame(columns=list(_agg))
        self._fvg_done_1hr = None
        self._fvg_done_4hr = None

        # Reset daily.json and recompute fixed levels.
        save_daily(copy.deepcopy(DEFAULT_DAILY))
        _daily_mod.run_daily_fixed(
            now, _combined, self._fvg_1hr, self._fvg_4hr, now.date()
        )

        # Seed initial day/week/session levels from combined bars.
        _state = load_daily()
        _liq = _state.get("liquidities", [])
        _now_ts = now
        if _now_ts.tzinfo is None:
            _now_ts = _now_ts.tz_localize("America/New_York")
        _live = compute_live_hl_mid(_combined, _now_ts)
        for _name in ("week_high", "week_low", "week_mid", "day_high", "day_low", "day_mid"):
            if _name in _live:
                _existing = next((l for l in _liq if l["name"] == _name), None)
                if _existing:
                    _existing["price"] = _live[_name]
                else:
                    _liq.append({"name": _name, "kind": "level", "price": _live[_name]})
        # Seed session highs/lows from completed sessions in combined bars.
        _today = now.date()
        for _sess in ("asia", "london", "ny_morning", "ny_evening"):
            _sbars = _session_bars(_combined, _sess, _today)
            if not _sbars.empty:
                for _suffix, _col in (("high", "High"), ("low", "Low")):
                    _key = f"{_sess}_{_suffix}"
                    _price = float(_sbars[_col].max() if _suffix == "high" else _sbars[_col].min())
                    _ex = next((l for l in _liq if l["name"] == _key), None)
                    if _ex:
                        _ex["price"] = _price
                    else:
                        _liq.append({"name": _key, "kind": "level", "price": _price})
        _state["liquidities"] = _liq
        save_daily(_state)

        # Write levels.json snapshot for plot_session.py / regression plots. Lands under
        # the current state prefix: the session folder for live, the per-run folder for
        # backtests — so a backtest never pollutes the shared live sessions area.
        import json as _json
        _levels_path = paths.state_dir() / "levels.json"
        _daily_state = load_daily()
        self._ext_levels = [
            (l["name"], float(l["price"]))
            for l in _daily_state.get("liquidities", [])
            if l.get("kind") == "level" and l.get("price") is not None
        ]
        _levels_path.write_text(
            _json.dumps({
                "liquidities": _daily_state.get("liquidities", []),
                "all_time_high": _global.get("all_time_high"),
            }, indent=2),
            encoding="utf-8",
        )

        self._last_daily_date = now.date()

    def on_session_start(
        self,
        now: pd.Timestamp,
        today_mnq_at_open: pd.DataFrame,
        force_reset: bool = False,
    ) -> None:
        """Call once at session open. Computes liquidities and optionally resets state."""
        from smt_state import (
            DEFAULT_HYPOTHESIS, DEFAULT_POSITION,
            load_position, save_hypothesis, save_position,
        )

        # Live (not in-memory): route the four state JSONs into the session folder so the
        # live writer never shares state files across worktrees. The orchestrator passes
        # ACT_STATE_DIR to this subprocess so BOTH processes resolve the identical folder
        # (guaranteed agreement, no independent date computation). Standalone runs fall
        # back to sessions/<date>. Backtests (in-memory) keep the state_dir set by the
        # backtest harness (per-run folder) — do not override it here.
        if not _smt_state._IN_MEMORY:
            _env_state = os.environ.get("ACT_STATE_DIR")
            if _env_state:
                paths.set_state_dir(_env_state)
            else:
                _d = _smt_state._SESSION_DATE or cme_session_date(now).isoformat()
                paths.set_state_dir(paths.sessions_dir() / _d)

        # Reset per-hypothesis tracking state on every session start.
        self._last_hyp_cautious = ("", "")
        self._hyp_formation_price = None
        self._accepted_level_sweeps = set()
        self._swept_levels_since_hyp = set()

        # Always run the daily/startup liquidity computation.
        self.on_daily_or_startup(now, today_mnq_at_open)
        self._daily_triggered = True

        # Reset hypothesis and position only when explicitly forced.
        if force_reset:
            save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))
            save_position(copy.deepcopy(DEFAULT_POSITION))
            # Run first hypothesis so direction is populated immediately after force-reset.
            _init_hyp_divs = _hyp_mod.run_hypothesis(
                now, today_mnq_at_open, self._hist_mes_1m,
                self._hist_mnq_1m, self._hist_mes_1m,
                hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
            )
            for _d in (_init_hyp_divs or []):
                self._emit(_d)
            return

        # No force_reset: run hypothesis to populate direction, then reconcile with active position.
        _init_hyp_divs = _hyp_mod.run_hypothesis(
            now, today_mnq_at_open, self._hist_mes_1m,
            self._hist_mnq_1m, self._hist_mes_1m,
            hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
        )
        for _d in (_init_hyp_divs or []):
            self._emit(_d)

        # Reconcile hypothesis direction with any active position.
        _has_active = bool(load_position().get("active"))
        if _has_active:
            _active = load_position().get("active", {})
            _pos_dir = _active.get("direction", "")
            _pos_hyp_dir = "down" if _pos_dir == "short" else ("up" if _pos_dir == "long" else "none")
            _new_hyp_dir = _smt_state.load_hypothesis().get("direction", "none")

            if _new_hyp_dir == "none":
                _hyp_snap = _smt_state.load_hypothesis()
                _hyp_snap["direction"] = _pos_hyp_dir
                _smt_state.save_hypothesis(_hyp_snap)
            elif _new_hyp_dir != _pos_hyp_dir:
                _last_price = (
                    float(today_mnq_at_open.iloc[-1]["Close"])
                    if not today_mnq_at_open.empty else 0.0
                )
                _pos_snap = load_position()
                _pos_snap["conf_bar_entry"] = {}
                _pos_snap["stop_entry"] = ""
                save_position(_pos_snap)
                self._emit({
                    "kind":             "trend-broken",
                    "time":             now.isoformat(),
                    "direction":        _new_hyp_dir,
                    "broken_direction": _pos_hyp_dir,
                    "level_name":       "session-restart",
                    "level_price":      "",
                    "price":            _last_price,
                })

    def on_1m_bar(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,
        mes_bar_row: pd.Series,
        today_mnq: pd.DataFrame,
        today_mes: pd.DataFrame,
    ) -> list[dict]:
        """Process one completed 1m bar. Returns list of emitted event dicts."""
        if not self._daily_triggered:
            return []

        # Re-run daily level computation at two transitions per CME session day.
        # 00:00 ET (London session start): today's midnight open is now available as TDO,
        #   replacing the prior-day proxy used during the Asia session.
        # 09:20 ET (NY pre-market): FVGs and session H/L are stable for the RTH session.
        # Only one fires per calendar date — whichever comes first sets _last_daily_date.
        _bar_floor = now.floor("1min")
        _is_midnight = (now.hour == 0 and now.minute == 0)
        _is_0920    = (now.hour == 9 and now.minute == 20)
        if ((_is_midnight or _is_0920)
                and _bar_floor != self._last_daily_minute
                and now.date() != self._last_daily_date):
            self._last_daily_minute = _bar_floor
            self.on_daily_or_startup(now, today_mnq)

        _o = float(mnq_bar_row["Open"])
        _h = float(mnq_bar_row["High"])
        _l = float(mnq_bar_row["Low"])
        _c = float(mnq_bar_row["Close"])

        # Fix #8: bar_dict always includes body_high / body_low.
        mnq_1m_bar = {
            "time": now.isoformat(),
            "open": _o, "high": _h, "low": _l, "close": _c,
            "body_high": max(_o, _c), "body_low": min(_o, _c),
        }

        # Fix #7: recent = all-day bars from midnight up to now.
        # today_mnq is already built so its last bar is <= now (both 1m and 1s callers),
        # making the mask a full-copy no-op in the common case. Skip the copy then —
        # run_trend / run_strategy treat recent strictly read-only.
        _tm_idx = today_mnq.index
        if len(_tm_idx) == 0 or _tm_idx[-1] <= now:
            recent = today_mnq
        else:
            recent = today_mnq[_tm_idx <= now]

        events: list[dict] = []

        # Window-entry guard: if a position was opened outside any entry window and we
        # just crossed into an allowed window, treat it as a market-close (log only —
        # no PMT order sent) so the strategy re-evaluates cleanly from a flat state.
        _now_et = now.tz_convert(_ET) if now.tzinfo else now
        _now_in_window = is_entry_allowed(_now_et.time())
        if self._was_in_window is not None and not self._was_in_window and _now_in_window:
            _ghost_pos = _smt_state.load_position()
            _ghost_active = _ghost_pos.get("active", {})
            if _ghost_active:
                _open_time_str = _ghost_active.get("time", "")
                _opened_outside = False
                if _open_time_str:
                    try:
                        _open_ts = pd.Timestamp(_open_time_str)
                        _open_et = _open_ts.tz_convert(_ET) if _open_ts.tzinfo else _open_ts
                        _opened_outside = not is_entry_allowed(_open_et.time())
                    except Exception:
                        pass
                if _opened_outside:
                    _ghost_evt: dict = {
                        "kind":      "market-close",
                        "time":      now.isoformat(),
                        "price":     float(_ghost_active.get("fill_price", 0)),
                        "reason":    "window-entered",
                        "direction": _ghost_active.get("direction", ""),
                        "source":    "strategy",
                    }
                    _ghost_hyp = _smt_state.load_hypothesis()
                    _ghost_pos["active"] = {}
                    _ghost_pos["stop_entry"] = ""
                    _ghost_pos["conf_bar_exit"] = {}
                    _ghost_hyp["direction"] = "none"
                    _ghost_hyp["manual"] = False  # GIL-8: direction cleared → release the lock
                    _smt_state.save_position(_ghost_pos)
                    _smt_state.save_hypothesis(_ghost_hyp)
                    self._emit(_ghost_evt)
                    events.append(_ghost_evt)
        self._was_in_window = _now_in_window

        # Snapshot direction before any module can mutate hypothesis state — ensures
        # terminal output and executor receive the same direction for every signal this bar.
        _hyp_dir = _smt_state.load_hypothesis().get("direction", "none")

        # Snapshot stop_entry before trend runs so we can detect silent cancellations.
        _prev_stop = _smt_state.load_position().get("stop_entry", "")

        # Trend runs first: validates existing hypothesis before a new one may form.
        trend_sig = _trend_mod.run_trend(now, mnq_1m_bar, recent)
        if trend_sig is not None:
            if trend_sig["kind"] == "level-swept":
                # A high-priority liquidity level was crossed. Re-evaluate hypothesis
                # from a neutral state so it makes an unbiased direction assessment.
                # Without clearing first, hypothesis is sticky and won't change an
                # existing direction unless the evidence is overwhelming.
                #
                # Cooldown active: the same level+direction was swept recently. Still
                # emit trend-broken to reset direction, but skip the immediate hypothesis
                # re-run — it will form naturally at the next 5m boundary.
                if _smt_state.load_hypothesis().get("manual"):
                    # GIL-8 manual direction lock (trade.py set-direction): a swept
                    # level must not reset the manually forced hypothesis — absorb the
                    # sweep as a non-event until trade.py unlock / trend-broken releases.
                    pass
                elif trend_sig.get("cooldown_active"):
                    _level_name_cd  = trend_sig.get("level_name", "")
                    _level_price_cd = float(trend_sig.get("level_price", 0) or 0)
                    # Suppress if the level was already priced in at hypothesis formation
                    # (formation close was on same side as level).
                    _suppress_tb = (
                        (bool(_level_name_cd) and (_level_name_cd, _hyp_dir) in self._accepted_level_sweeps)
                        or (
                            self._hyp_formation_price is not None
                            and _level_price_cd > 0
                            and (
                                (_hyp_dir == "up"   and self._hyp_formation_price <= _level_price_cd)
                                or (_hyp_dir == "down" and self._hyp_formation_price >= _level_price_cd)
                            )
                        )
                    )
                    # Aggression override: if the current bar is significantly more than
                    # formation_offset past the level, the level is being genuinely violated
                    # (e.g. Apr 16: 84 pts vs 5 pt formation offset). Fire trend-broken.
                    if _suppress_tb and _level_price_cd > 0 and self._hyp_formation_price is not None:
                        _bar_low_cd  = float(trend_sig.get("bar_low",  _level_price_cd) or _level_price_cd)
                        _bar_high_cd = float(trend_sig.get("bar_high", _level_price_cd) or _level_price_cd)
                        if _hyp_dir == "up":
                            _form_off = _level_price_cd - self._hyp_formation_price
                            _cur_off  = _level_price_cd - _bar_low_cd
                        else:
                            _form_off = self._hyp_formation_price - _level_price_cd
                            _cur_off  = _bar_high_cd - _level_price_cd
                        if _cur_off > _form_off + self._COOLDOWN_AGGRESSION_PTS:
                            _suppress_tb = False
                    if not _suppress_tb:
                        _hyp_snap = _smt_state.load_hypothesis()
                        _hyp_snap["direction"] = "none"
                        _smt_state.save_hypothesis(_hyp_snap)
                        _pos = _smt_state.load_position()
                        _pos["conf_bar_entry"] = {}
                        _pos["stop_entry"] = ""
                        _smt_state.save_position(_pos)
                        _tb_sig = {
                            "kind":             "trend-broken",
                            "time":             trend_sig["time"],
                            "direction":        "none",
                            "broken_direction": _hyp_dir,
                            "level_name":       trend_sig.get("level_name", ""),
                            "level_price":      trend_sig.get("level_price", ""),
                            "price":            trend_sig["price"],
                            "cooldown_active":  True,
                        }
                        for _k in ("bar_low", "bar_high"):
                            if _k in trend_sig:
                                _tb_sig[_k] = trend_sig[_k]
                        self._emit(_tb_sig)
                        events.append(_tb_sig)
                        if _prev_stop != "":
                            _cancel_sig = {
                                "kind":      "stop-entry-cancelled",
                                "time":      now.isoformat(),
                                "price":     float(_prev_stop),
                                "reason":    "trend-broken",
                                "direction": _hyp_dir,
                            }
                            self._emit(_cancel_sig)
                            events.append(_cancel_sig)
                        _hyp_dir = "none"
                        self._hyp_formation_price = None
                        self._accepted_level_sweeps.clear()
                        self._swept_levels_since_hyp.clear()
                    _level_hyp_divs = []
                else:
                    _hyp_snap = _smt_state.load_hypothesis()
                    _hyp_snap["direction"] = "none"
                    _smt_state.save_hypothesis(_hyp_snap)
                    _cur_1m = now.floor("1min")
                    if _cur_1m != self._last_trend_hyp_1m:
                        self._last_trend_hyp_1m = _cur_1m
                        _level_hyp_divs = _hyp_mod.run_hypothesis(
                            now, today_mnq, today_mes,
                            self._hist_mnq_1m, self._hist_mes_1m,
                            hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
                            skip_position_reset=True,
                        )
                    else:
                        _level_hyp_divs = []
                    _new_dir = _smt_state.load_hypothesis().get("direction", "none")
                    if _new_dir == "none":
                        # Hypothesis couldn't form after level sweep. Two cases:
                        # (a) Bar is entirely above ATH — price in uncharted territory
                        #     where no new hypothesis can form. Treat as a real break:
                        #     emit trend-broken, leave direction as "none".
                        # (b) Genuinely indeterminate — restore original direction
                        #     and preserve limits (non-event).
                        _ath = _smt_state.load_global().get("all_time_high")
                        # Use the same 5m bar window as hypothesis.py's _build_5m_bar:
                        # floor now to previous 5m boundary, take bars in [bar_start, bar_end).
                        _above_ath = False
                        if _ath is not None:
                            _ts = pd.Timestamp(now)
                            _bar_end = _ts.replace(
                                minute=(_ts.minute // 5) * 5, second=0, microsecond=0
                            )
                            _bar_start = _bar_end - pd.Timedelta(minutes=5)
                            _5m_win = today_mnq[
                                (today_mnq.index >= _bar_start) & (today_mnq.index < _bar_end)
                            ]
                            if not _5m_win.empty:
                                _above_ath = (
                                    float(_5m_win["Low"].min())  > float(_ath)
                                    and float(_5m_win["High"].max()) > float(_ath)
                                )
                        if not _above_ath:
                            _hyp_snap2 = _smt_state.load_hypothesis()
                            _hyp_snap2["direction"] = _hyp_dir
                            _smt_state.save_hypothesis(_hyp_snap2)
                            _new_dir = _hyp_dir
                    if _new_dir != _hyp_dir:
                        # Direction changed — real trend break. Clear pending limits and emit
                        # trend-broken so the automation path can cancel the PMT order.
                        self._accepted_level_sweeps.clear()
                        self._swept_levels_since_hyp.clear()
                        _pos = _smt_state.load_position()
                        _pos["conf_bar_entry"] = {}
                        _pos["stop_entry"] = ""
                        _smt_state.save_position(_pos)
                        _tb_sig: dict = {
                            "kind":             "trend-broken",
                            "time":             trend_sig["time"],
                            "direction":        _new_dir,
                            "broken_direction": _hyp_dir,
                            "level_name":       trend_sig.get("level_name", ""),
                            "level_price":      trend_sig.get("level_price", ""),
                            "price":            trend_sig["price"],
                        }
                        for _k in ("bar_low", "bar_high"):
                            if _k in trend_sig:
                                _tb_sig[_k] = trend_sig[_k]
                        self._emit(_tb_sig)
                        events.append(_tb_sig)
                        if _prev_stop != "":
                            _cancel_sig = {
                                "kind":      "stop-entry-cancelled",
                                "time":      now.isoformat(),
                                "price":     float(_prev_stop),
                                "reason":    "trend-broken",
                                "direction": _hyp_dir,
                            }
                            self._emit(_cancel_sig)
                            events.append(_cancel_sig)
                    else:
                        # Direction unchanged — this level sweep was absorbed without flipping.
                        # Record it so a subsequent cooldown sweep of the same level is suppressed.
                        _level_name_nc = trend_sig.get("level_name", "")
                        if _level_name_nc and _hyp_dir != "none":
                            self._accepted_level_sweeps.add((_level_name_nc, _hyp_dir))
                    # Emit any hypothesis signals (new-hypothesis, smt-div, etc.).
                    # If direction was unchanged, limits are preserved — no trend-broken emitted.
                    # Above session ATH with direction=down, suppress new-hypothesis if
                    # cautious prices haven't changed (direction-unchanged sweeps are common
                    # there and would otherwise produce a stream of identical signals).
                    _ls_above_ath = (
                        self._session_ath is not None
                        and float(mnq_bar_row["High"]) >= float(self._session_ath)
                    )
                    _dir_unchanged = (_new_dir == _hyp_dir)
                    for _d in (_level_hyp_divs or []):
                        if (
                            _d.get("kind") == "new-hypothesis"
                            and _new_dir == "down"
                            and _ls_above_ath
                            and _dir_unchanged
                        ):
                            _new_c = (
                                _d.get("cautious_price_initial", ""),
                                _d.get("cautious_price_secondary", ""),
                            )
                            if _new_c == self._last_hyp_cautious:
                                continue
                            self._last_hyp_cautious = _new_c
                        elif _d.get("kind") == "new-hypothesis" and _new_dir == "down" and _ls_above_ath:
                            # Direction changed AND above ATH: update tracker but always emit.
                            self._last_hyp_cautious = (
                                _d.get("cautious_price_initial", ""),
                                _d.get("cautious_price_secondary", ""),
                            )
                        if _d.get("kind") == "new-hypothesis" and _new_dir != _hyp_dir:
                            # Only update when direction actually changed; if unchanged,
                            # preserve the original formation price so cooldown suppression
                            # can still check whether the level was already past at first formation.
                            self._hyp_formation_price = _c
                        self._emit(_d)
                        events.append(_d)
                    _hyp_dir = _new_dir
            elif trend_sig["kind"] == "ath-crossed":
                # Bar straddled the fixed session_ath (09:20 ATH) with direction not
                # "down". First time into uncharted territory — emit trend-broken,
                # re-run hypothesis (will form with direction="down"), cancel limits.
                _cur_1m = now.floor("1min")
                if _cur_1m != self._last_trend_hyp_1m:
                    self._last_trend_hyp_1m = _cur_1m
                    _ath_hyp_divs = _hyp_mod.run_hypothesis(
                        now, today_mnq, today_mes,
                        self._hist_mnq_1m, self._hist_mes_1m,
                        hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
                        skip_position_reset=True,
                    )
                else:
                    _ath_hyp_divs = []
                _new_dir = _smt_state.load_hypothesis().get("direction", "none")
                _tb_sig = {
                    "kind":             "trend-broken",
                    "time":             trend_sig["time"],
                    "direction":        _new_dir,
                    "broken_direction": _hyp_dir,
                    "level_name":       "ath",
                    "level_price":      trend_sig.get("all_time_high", ""),
                    "price":            trend_sig["price"],
                    "bar_high":         trend_sig.get("bar_high", ""),
                    "bar_low":          trend_sig.get("bar_low", ""),
                }
                self._emit(_tb_sig)
                events.append(_tb_sig)
                if _prev_stop != "":
                    _cancel_sig = {
                        "kind":      "stop-entry-cancelled",
                        "time":      now.isoformat(),
                        "price":     float(_prev_stop),
                        "reason":    "trend-broken",
                        "direction": _hyp_dir,
                    }
                    self._emit(_cancel_sig)
                    events.append(_cancel_sig)
                self._hyp_formation_price = None
                self._accepted_level_sweeps.clear()
                self._swept_levels_since_hyp.clear()
                for _d in (_ath_hyp_divs or []):
                    self._emit(_d)
                    if _d.get("kind") == "new-hypothesis":
                        self._last_hyp_cautious = (
                            _d.get("cautious_price_initial", ""),
                            _d.get("cautious_price_secondary", ""),
                        )
                        self._hyp_formation_price = _c
                events.extend(_ath_hyp_divs or [])
                _hyp_dir = _new_dir
            elif trend_sig["kind"] == "dynamic-ath-crossed":
                # Already above session_ath with direction="down". The running high
                # was straddled — cautious prices may have shifted. Re-run hypothesis
                # and emit new-hypothesis ONLY if cautious prices changed. No trend-broken.
                _cur_1m = now.floor("1min")
                if _cur_1m != self._last_trend_hyp_1m:
                    self._last_trend_hyp_1m = _cur_1m
                    _dath_hyp_divs = _hyp_mod.run_hypothesis(
                        now, today_mnq, today_mes,
                        self._hist_mnq_1m, self._hist_mes_1m,
                        hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
                        skip_position_reset=True,
                    )
                else:
                    _dath_hyp_divs = []
                for _d in (_dath_hyp_divs or []):
                    if _d.get("kind") == "new-hypothesis":
                        _new_c = (
                            _d.get("cautious_price_initial", ""),
                            _d.get("cautious_price_secondary", ""),
                        )
                        if _new_c != self._last_hyp_cautious:
                            self._emit(_d)
                            events.append(_d)
                            self._last_hyp_cautious = _new_c
                            self._hyp_formation_price = _c
                    else:
                        self._emit(_d)
                        events.append(_d)
                _hyp_dir = _smt_state.load_hypothesis().get("direction", "none")
            else:
                # Normal trend signal (daily/weekly mid invalidation, or cautious exit).
                trend_sig.setdefault("direction", _hyp_dir)
                self._emit(trend_sig)
                events.append(trend_sig)
                # Emit a dedicated cancel signal if trend cleared a pending limit without one.
                if _prev_stop != "" and _smt_state.load_position().get("stop_entry", "") == "":
                    _cancel_sig = {
                        "kind":      "stop-entry-cancelled",
                        "time":      now.isoformat(),
                        "price":     float(_prev_stop),
                        "reason":    trend_sig.get("kind", "trend-broken"),
                        "direction": _hyp_dir,
                    }
                    self._emit(_cancel_sig)
                    events.append(_cancel_sig)

        # Liquidity-sweep decrement: the first time a named liquidity level is straddled
        # by the current bar under the current hypothesis direction, and no position is
        # active, decrement failed_entries by 1 (if at the limit).  This allows one
        # additional re-entry after each genuinely new liquidity is tapped — covering
        # today's levels and the prev1/prev2 levels already included in _ext_levels.
        if _hyp_dir != "none" and self._ext_levels:
            _xpos = _smt_state.load_position()
            if not _xpos.get("active"):
                _xbl = float(mnq_bar_row["Low"])
                _xbh = float(mnq_bar_row["High"])
                _xdecremented = False
                for _xln, _xlp in self._ext_levels:
                    if _xln in self._swept_levels_since_hyp:
                        continue
                    if _xbl <= _xlp <= _xbh:
                        self._swept_levels_since_hyp.add(_xln)
                        if not _xdecremented:
                            _xfe = _xpos.get("failed_entries", 0)
                            if _xfe >= _strat_mod.MAX_FAILED_ENTRIES:
                                _xpos["failed_entries"] = _xfe - 1
                                _smt_state.save_position(_xpos)
                                _xdecremented = True

        # Per-bar: update session/day/week H/L and prune visited FVGs.
        self._update_dynamic_liquidities(now, mnq_bar_row, today_mnq)

        _this_5m = now.floor("5min")
        is_5m = (now.minute % 5 == 0) and (_this_5m != self._last_5m_processed)

        if is_5m:
            self._last_5m_processed = _this_5m
            # Fix #4: all-day MNQ/MES slices (midnight to now).
            # Fix #3: pass hist_1hr and hist_4hr.
            hyp_divs = _hyp_mod.run_hypothesis(
                now,
                today_mnq,
                today_mes,
                self._hist_mnq_1m,
                self._hist_mes_1m,
                hist_1hr=self._hist_1hr,
                hist_4hr=self._hist_4hr,
            )
            if hyp_divs:
                _above_session_ath = (
                    self._session_ath is not None
                    and float(mnq_bar_row["High"]) >= float(self._session_ath)
                )
                for d in hyp_divs:
                    if d.get("kind") == "new-hypothesis" and _hyp_dir == "down" and _above_session_ath:
                        _new_c = (
                            d.get("cautious_price_initial", ""),
                            d.get("cautious_price_secondary", ""),
                        )
                        if _new_c == self._last_hyp_cautious:
                            continue
                        self._last_hyp_cautious = _new_c
                    if d.get("kind") == "new-hypothesis":
                        self._hyp_formation_price = _c
                    self._emit(d)
                    events.append(d)
            # Reload direction so strategy sees the updated bias on the same bar.
            _new_5m_dir = _smt_state.load_hypothesis().get("direction", "none")
            if _new_5m_dir != _hyp_dir:
                self._accepted_level_sweeps.clear()
                self._swept_levels_since_hyp.clear()
            _hyp_dir = _new_5m_dir

        # Fix #1: run_strategy on every 1m bar; full entry logic only at 5m boundaries.
        # After an exit, force-eval re-opens the entry gate before the next 5m boundary.
        # market-close fires immediately; stopped-out waits for the next minute boundary
        # to prevent the 1s-mode cascade (re-fill + re-stop within the same second).
        _force_eval_now = (
            self._force_entry_eval_after is not None
            and now >= self._force_entry_eval_after
        )
        _run_full_entry = is_5m or _force_eval_now
        _prefer_mkt = False
        if _force_eval_now:
            self._force_entry_eval_after = None
            _prefer_mkt = self._force_market_entry
            self._force_market_entry = False
        # Pause (entry-side): while paused, suppress ALL entry-side strategy work when flat —
        # not just the broker dispatch. run_strategy writes stop_entry/active to position.json
        # directly and can fill-detect a stale stop_entry into a *phantom* position, so it must
        # be skipped entirely when there is no active position. A real active position is still
        # managed (run_strategy Section 3 runs) so exits keep working.
        if _smt_state.is_paused() and not _smt_state.load_position().get("active"):
            strat_sig = None
        else:
            strat_sig = _strat_mod.run_strategy(now, mnq_1m_bar, recent,
                                                 fill_check_only=not _run_full_entry,
                                                 prefer_market_entry=_prefer_mkt)
        if strat_sig is not None:
            strat_sig.setdefault("direction", _hyp_dir)
            # Emit cancel when strategy's market-entry overwrites a pending limit that
            # was never explicitly cancelled by trend (trend cancel path handled above).
            # Also mark prefer_market_entry re-entries so live_orders can flatten first —
            # Tradovate's protective stop may not have settled by the time the market buy arrives.
            if strat_sig["kind"] == "market-entry" and _prefer_mkt:
                strat_sig["flatten_first"] = True
            if strat_sig["kind"] == "market-entry" and _prev_stop != "":
                _cancel_sig = {
                    "kind":      "stop-entry-cancelled",
                    "time":      now.isoformat(),
                    "price":     float(_prev_stop),
                    "reason":    "market-entry",
                    "direction": _hyp_dir,
                }
                self._emit(_cancel_sig)
                events.append(_cancel_sig)
            self._emit(strat_sig)
            events.append(strat_sig)
            if strat_sig["kind"] == "market-close":
                # Handles the rare direction-mismatch path from strategy.py.
                # The main cautiousbreak path comes from trend_sig and is handled above.
                self._force_entry_eval_after = now.floor("1min")
                self._force_market_entry = True
            elif strat_sig["kind"] == "stopped-out":
                self._force_entry_eval_after = now.floor("1min") + pd.Timedelta(minutes=1)

            # Same-bar stop check for LONG (up) entries: when entry fills on bar N,
            # the same bar's Low may already breach the protective stop. In 1s
            # resolution this is caught naturally (price dips on the very next
            # second bar), but in 1m mode the 60s bar aggregates the dip and the
            # stop check doesn't run until bar N+1.
            #
            # Only applies to UP entries. For DOWN entries the bar's High is
            # typically reached BEFORE the entry fills (price spikes high, then
            # falls to fill the stop-entry), so checking bar_high >= stop would
            # fire against a pre-fill spike rather than a post-fill reversal.
            if strat_sig["kind"] in {"stop-entry-filled", "market-entry"}:
                _sbsc_pos = _smt_state.load_position()
                _sbsc_active = _sbsc_pos.get("active", {})
                _sbsc_stop = _sbsc_active.get("stop")
                _sbsc_dir = _sbsc_active.get("direction", "")
                if _sbsc_stop is not None and _sbsc_dir == "up" and _l <= _sbsc_stop:
                    _sbsc_pos["active"] = {}
                    _sbsc_pos["stop_entry"] = ""
                    _sbsc_pos["failed_entries"] = _sbsc_pos.get("failed_entries", 0) + 1
                    _smt_state.save_position(_sbsc_pos)
                    _sbsc_sig = {
                        "kind":      "stopped-out",
                        "time":      now.isoformat(),
                        "price":     float(_sbsc_stop),
                        "direction": _sbsc_dir,
                    }
                    self._emit(_sbsc_sig)
                    events.append(_sbsc_sig)
                    self._force_entry_eval_after = now.floor("1min") + pd.Timedelta(minutes=1)

        self._write_bar_state(now, today_mnq)
        return events

    def _update_dynamic_liquidities(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,
        today_mnq: pd.DataFrame,
    ) -> list[dict]:
        """Update session/day/week H/L and FVG state in daily.json on every bar."""
        from daily import _session_bars, TIME_WINDOWS

        _state = _smt_state.load_daily()
        _liq = _state.get("liquidities", [])
        _liq_map = {l["name"]: l for l in _liq}

        _bar_high = float(mnq_bar_row["High"])
        _bar_low  = float(mnq_bar_row["Low"])
        _changed: list[str] = []
        _liq_events: list[dict] = []

        # _hist_mnq_1m is constant per session and strictly precedes today_mnq, so cache an
        # 8-day tail once (covers the widest lookback: week H/L ≤5d, day H/L to 06:00).
        if self._dyn_hist_tail is None:
            _tail_cut = _cme_session_start(now) - pd.Timedelta(days=8)
            self._dyn_hist_tail = self._hist_mnq_1m[self._hist_mnq_1m.index >= _tail_cut]
        _tail = self._dyn_hist_tail

        # today_mnq rows are all <= now (last is the running partial bar), so its plain
        # max/min IS the [session_start, now] window extreme — shared by day + week below.
        _th_vals = today_mnq["High"].values
        _tl_vals = today_mnq["Low"].values
        _today_hi = float(_th_vals.max()) if len(_th_vals) else None
        _today_lo = float(_tl_vals.min()) if len(_tl_vals) else None

        # Helper: update a named level, track change.
        def _set(name: str, price: float, kind: str = "level") -> None:
            if name in _liq_map:
                if abs(_liq_map[name].get("price", 0) - price) > 1e-9:
                    _old = _liq_map[name].get("price")
                    _liq_map[name]["price"] = price
                    _changed.append(name)
                    _liq_events.append({
                        "kind": "liquidity-updated",
                        "time": now.isoformat(),
                        "name": name,
                        "old_price": _old,
                        "price": price,
                    })
            else:
                _new_entry = {"name": name, "kind": kind, "price": price}
                _liq_map[name] = _new_entry
                _liq.append(_new_entry)
                _changed.append(name)
                _liq_events.append({
                    "kind": "liquidity-updated",
                    "time": now.isoformat(),
                    "name": name,
                    "old_price": None,
                    "price": price,
                })

        # ── Day H/L ──────────────────────────────────────────────────────────
        # Early sessions extend the day H/L lookback ~2 sessions back using hist bars,
        # starting on the session-open day at:
        #   Asia   (≥18:00 ET): 06:00 ET — prior NY morning open
        #   London (<06:00 ET): 12:00 ET — prior NY evening open
        # From 06:00 ET onwards (NY morning+) use only the current CME session (today_mnq).
        _now_hour = now.hour
        if _now_hour >= 18 or _now_hour < 6:
            _sess_open_day = _cme_session_start(now).date()
            _ext_hour = 6 if _now_hour >= 18 else 12   # Asia→prior NY morning; London→prior NY evening
            _day_start_ts = pd.Timestamp(
                datetime.datetime(_sess_open_day.year, _sess_open_day.month, _sess_open_day.day, _ext_hour, 0),
                tz="America/New_York",
            )
            # Constant hist contribution [day_start, session_start) — recompute only when
            # the (session-stable) day_start changes.
            if self._dyn_day_key != _day_start_ts:
                self._dyn_day_key = _day_start_ts
                _dh_hist = _tail[_tail.index >= _day_start_ts]
                self._dyn_day_hl = (
                    (float(_dh_hist["High"].values.max()), float(_dh_hist["Low"].values.min()))
                    if not _dh_hist.empty else (None, None)
                )
            _dh = _mmax(self._dyn_day_hl[0], _today_hi)
            _dl = _mmin(self._dyn_day_hl[1], _today_lo)
        else:
            _dh = _today_hi
            _dl = _today_lo
        if _dh is not None and _dl is not None:
            _set("day_high", _dh)
            _set("day_low",  _dl)
            _set("day_mid",  (_dh + _dl) / 2.0)

        # ── Week H/L ───────────────────────────────────────────────────────────
        # Week may span multiple sessions; the constant hist contribution
        # [week_start, session_start) is computed once and combined with today's extreme.
        _week_start = self._week_start_ts(now)
        if self._dyn_week_key != _week_start:
            self._dyn_week_key = _week_start
            _wh_hist = _tail[_tail.index >= _week_start]
            self._dyn_week_hl = (
                (float(_wh_hist["High"].values.max()), float(_wh_hist["Low"].values.min()))
                if not _wh_hist.empty else (None, None)
            )
        _wh = _mmax(self._dyn_week_hl[0], _today_hi)
        _wl = _mmin(self._dyn_week_hl[1], _today_lo)
        if _wh is not None and _wl is not None:
            _set("week_high", _wh)
            _set("week_low",  _wl)
            _set("week_mid",  (_wh + _wl) / 2.0)

        # ── Active session H/L ─────────────────────────────────────────────────
        _hour, _minute = now.hour, now.minute
        _t = _hour * 60 + _minute
        if _t >= 18 * 60:
            _active_sess = "asia"
        elif _t < 6 * 60:
            _active_sess = "london"
        elif _t < 12 * 60:
            _active_sess = "ny_morning"
        elif _t < 17 * 60:
            _active_sess = "ny_evening"
        else:
            _active_sess = None  # maintenance 17:00-18:00

        if _active_sess is not None:
            # Asia crosses midnight: 18:00 prior calendar day → 00:00 current day.
            # For bars in 18:00–00:00 ET, now.date() is still the prior day, so
            # _session_bars needs tomorrow as its `today` to build the correct window.
            _sess_today = (
                now.date() + datetime.timedelta(days=1)
                if _active_sess == "asia"
                else now.date()
            )
            # Only the asia window (18:00–00:00) reaches before session open; it needs the
            # ~5 pre-18:05 hist bars. Feed _session_bars that constant sliver + today_mnq
            # instead of the full combined frame — every active-session window is otherwise
            # contained in today_mnq.
            if self._dyn_sliver18 is None:
                _s18 = pd.Timestamp(_cme_session_start(now))
                self._dyn_sliver18 = _tail[_tail.index >= _s18]
            _sess_src = (
                pd.concat([self._dyn_sliver18, today_mnq])
                if not self._dyn_sliver18.empty else today_mnq
            )
            _sbars = _session_bars(_sess_src, _active_sess, _sess_today)
            if not _sbars.empty:
                _sh = float(_sbars["High"].max())
                _sl = float(_sbars["Low"].min())
                _set(f"{_active_sess}_high", _sh)
                _set(f"{_active_sess}_low", _sl)

        # ── FVG visited prune ──────────────────────────────────────────────────
        _to_remove = []
        for _l in _liq:
            if _l.get("kind") != "fvg":
                continue
            _ftop = _l.get("top")
            _fbot = _l.get("bottom")
            if _ftop is None or _fbot is None:
                continue
            # Bar straddles the FVG zone → visited
            if _bar_high >= _fbot and _bar_low <= _ftop:
                _to_remove.append(_l["name"])

        if _to_remove:
            _liq[:] = [l for l in _liq if l["name"] not in _to_remove]
            # Rebuild map after removal
            _liq_map = {l["name"]: l for l in _liq}
            for _removed in _to_remove:
                _liq_events.append({
                    "kind": "liquidity-updated",
                    "time": now.isoformat(),
                    "name": _removed,
                    "old_price": None,
                    "price": None,
                    "visited": True,
                })

        # ── New FVG detection from LIVE bars at 1hr/4hr boundaries ─────────────
        # Frozen-frame fix (2026-06-05): the old scan re-ran _detect_fvgs over
        # self._hist_1hr/_hist_4hr — frames frozen at session init — so FVGs forming
        # intra-session were never detected, and hist FVGs already excluded as
        # visited could resurrect (that re-scan's visited check only saw today's
        # bars). Now each just-completed TF bar is resampled from live 1m and only
        # the 3-bar windows ending at new bars are tested.
        for _f in self._extend_fvg_frames(now, today_mnq):
            if _f["name"] not in _liq_map:
                _liq.append(_f)
                _liq_map[_f["name"]] = _f
                _changed.append(_f["name"])

        if _changed or _to_remove:
            _state["liquidities"] = list(_liq_map.values())
            _smt_state.save_daily(_state)
            # Keep _ext_levels current for the sweep check.
            self._ext_levels = [
                (l["name"], float(l["price"]))
                for l in _state["liquidities"]
                if l.get("kind") == "level" and l.get("price") is not None
            ]

        return _liq_events

    def _extend_fvg_frames(self, now: pd.Timestamp, today_mnq: pd.DataFrame) -> list[dict]:
        """Append just-completed 1hr/4hr bars (built from live 1m) to the rolling FVG
        frames and return the FVGs completed by those new bars.

        Boundary tracking is timestamp-based rather than minute==0, so a missed tick
        or a data gap catches up on every boundary since the last processed one. Only
        the 3-bar windows ending at newly appended bars are tested — an FVG can only
        ever complete at its 3rd bar, so older windows need no re-scan (and a pruned
        FVG can never resurrect). The visited check inside _detect_fvgs runs against
        today's 1m bars after formation, matching the daily-scan semantics.
        """
        from daily import _detect_fvgs

        out: list[dict] = []
        for _freq, _frame_attr, _done_attr in (
            ("1h", "_fvg_1hr", "_fvg_done_1hr"),
            ("4h", "_fvg_4hr", "_fvg_done_4hr"),
        ):
            _cur = now.floor(_freq)  # bars labeled >= _cur are still forming
            if getattr(self, _done_attr) == _cur:
                continue  # fast path: no new boundary completed since the last call
            _frame = getattr(self, _frame_attr)
            if _frame is None:
                continue
            setattr(self, _done_attr, _cur)
            if today_mnq.empty:
                continue
            _step = pd.Timedelta(_freq)
            # Completed-but-missing labels: from the frame's end (or today's first
            # bar after a blank frame) up to — excluding — the still-forming bar.
            _label = (
                _frame.index[-1] + _step
                if len(_frame)
                else today_mnq.index[0].floor(_freq)
            )
            _new_idx: list = []
            _new_rows: list[dict] = []
            while _label < _cur:
                _lo = today_mnq.index.searchsorted(_label, side="left")
                _hi = today_mnq.index.searchsorted(_label + _step, side="left")
                _win = today_mnq.iloc[_lo:_hi]
                if not _win.empty:
                    _new_idx.append(_label)
                    _new_rows.append({
                        "Open":  float(_win["Open"].iloc[0]),
                        "High":  float(_win["High"].values.max()),
                        "Low":   float(_win["Low"].values.min()),
                        "Close": float(_win["Close"].iloc[-1]),
                    })
                _label += _step
            if not _new_rows:
                continue
            _add = pd.DataFrame(_new_rows, index=pd.DatetimeIndex(_new_idx))
            _frame = _add if _frame.empty else pd.concat([_frame, _add])
            setattr(self, _frame_attr, _frame)
            # tail(K+2): exactly the windows whose completing (3rd) bar is new.
            out.extend(_detect_fvgs(_frame.tail(len(_new_rows) + 2), today_mnq))
        return out

    def _day_start_ts(self, now: pd.Timestamp) -> pd.Timestamp:
        """Return 18:00 ET on the date the current CME futures session opened.

        After 18:00 ET today the new session opened today at 18:00.
        Before 18:00 ET the session opened yesterday at 18:00.
        """
        now_et = now.tz_convert("America/New_York") if now.tzinfo else now.tz_localize("America/New_York")
        d = now_et.date()
        if now_et.hour < 18:
            d -= datetime.timedelta(days=1)
        return pd.Timestamp(
            datetime.datetime(d.year, d.month, d.day, 18, 0),
            tz="America/New_York",
        )

    def _week_start_ts(self, now: pd.Timestamp) -> pd.Timestamp:
        """Return the extended week-H/L start for the CME session containing `now`.

        Monday session  (session-open = Sunday) → prev Thursday 18:00 ET
        Tuesday session (session-open = Monday) → prev Friday   18:00 ET
        Wednesday+      → standard Sunday 18:00 ET (CME week open)
        """
        today = now.date()
        _session_open = today if now.hour >= 18 else today - datetime.timedelta(days=1)
        _wd = _session_open.weekday()  # Mon=0, Tue=1, ..., Sun=6

        if _wd == 6:  # Sunday → Monday session
            _anchor = _session_open - datetime.timedelta(days=3)   # prev Thursday
        elif _wd == 0:  # Monday → Tuesday session
            _anchor = _session_open - datetime.timedelta(days=3)   # prev Friday
        else:
            _days_to_sunday = (_wd + 1) % 7
            _anchor = _session_open - datetime.timedelta(days=_days_to_sunday)
        return pd.Timestamp(
            datetime.datetime(_anchor.year, _anchor.month, _anchor.day, 18, 0),
            tz="America/New_York",
        )

    def _write_bar_state(self, now: pd.Timestamp, today_mnq: pd.DataFrame) -> None:
        current_5m = now.floor("5min")
        # Within a 5m block the [prev_5m, current_5m) window holds only completed,
        # immutable prior-block bars — recompute the stops once per block, then reuse.
        if current_5m != self._bar_state_5m:
            self._bar_state_5m = current_5m
            prev_5m = current_5m - pd.Timedelta(minutes=5)
            window = today_mnq[(today_mnq.index >= prev_5m) & (today_mnq.index < current_5m)]
            if window.empty:
                self._bar_state_vals = (None, None)
            else:
                bar_open  = float(window.iloc[0]["Open"])
                bar_close = float(window.iloc[-1]["Close"])
                bar_high  = float(window["High"].max())
                bar_low   = float(window["Low"].min())
                body_high = max(bar_open, bar_close)
                body_low  = min(bar_open, bar_close)
                self._bar_state_vals = (
                    round(max(bar_low,  body_low  - self._STOP_WICK_CAP), 4),
                    round(min(bar_high, body_high + self._STOP_WICK_CAP), 4),
                )
        _pl, _ps = self._bar_state_vals
        save_bar_state({
            "time": now.isoformat(),
            "potential_stop_long":  _pl,
            "potential_stop_short": _ps,
        })
