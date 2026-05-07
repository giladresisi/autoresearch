# session_pipeline.py
# Shared per-session bar-dispatch pipeline used by backtest_smt, signal_smt, automation.
# Fixes 8 live/backtest behavioral divergences by implementing the correct behavior once.
from __future__ import annotations

import copy
from typing import Callable

import pandas as pd

import daily as _daily_mod
import hypothesis as _hyp_mod
import smt_state as _smt_state
import strategy as _strat_mod
import trend as _trend_mod


class SessionPipeline:
    """Dispatches daily → trend → hypothesis → strategy for one trading session.

    Fixes: ATH seeding, hist_1hr/4hr to hypothesis, run_strategy every 1m bar,
    bar_dict body fields, consistent hourly resample, all-day 'recent' scope.
    """

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
        self._session_ath: float | None = None
        # Tracks (cautious_price_initial, cautious_price_secondary) of last emitted
        # new-hypothesis. Used to suppress redundant signals above session_ath.
        self._last_hyp_cautious: tuple[str, str] = ("", "")

    def on_session_start(
        self,
        now: pd.Timestamp,
        today_mnq_at_open: pd.DataFrame,
    ) -> None:
        """Seed ATH, reset state, compute resamples, call run_daily. Call once at 09:20 ET."""
        # Deferred import: tests monkeypatch smt_state path attributes before calling this
        # method, so importing at module level would capture the un-patched paths too early.
        from smt_state import (
            DEFAULT_DAILY, DEFAULT_GLOBAL, DEFAULT_HYPOTHESIS, DEFAULT_POSITION,
            save_daily, save_global, save_hypothesis, save_position,
        )

        # Fix #2: Seed ATH from full history before resetting state.
        seeded_global = copy.deepcopy(DEFAULT_GLOBAL)
        if not self._hist_mnq_1m.empty:
            _ath_seed = float(self._hist_mnq_1m["High"].max())
            seeded_global["all_time_high"] = _ath_seed
            # session_ath is fixed at 09:20 — never updated intraday.
            seeded_global["session_ath"] = _ath_seed
            self._session_ath = _ath_seed
        else:
            self._session_ath = None
        self._last_hyp_cautious = ("", "")
        save_global(seeded_global)
        save_daily(copy.deepcopy(DEFAULT_DAILY))
        save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))
        save_position(copy.deepcopy(DEFAULT_POSITION))

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
            self._hist_4hr = (
                self._hist_mnq_1m.resample("4h", label="left")
                .agg(_agg)
                .dropna(subset=["Open"])
            )
        else:
            self._hist_1hr = pd.DataFrame(columns=list(_agg))
            self._hist_4hr = pd.DataFrame(columns=list(_agg))

        # Fix #6: Pass only bars up to now (≤ 09:20) to run_daily.
        _daily_mod.run_daily(now, today_mnq_at_open, self._hist_mnq_1m, self._hist_1hr)
        self._daily_triggered = True

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
        recent = today_mnq[today_mnq.index <= now]

        events: list[dict] = []

        # Snapshot direction before any module can mutate hypothesis state — ensures
        # terminal output and executor receive the same direction for every signal this bar.
        _hyp_dir = _smt_state.load_hypothesis().get("direction", "none")

        # Snapshot limit_entry before trend runs so we can detect silent cancellations.
        _prev_limit = _smt_state.load_position().get("limit_entry", "")

        # Trend runs first: validates existing hypothesis before a new one may form.
        trend_sig = _trend_mod.run_trend(now, mnq_1m_bar, recent)
        if trend_sig is not None:
            if trend_sig["kind"] == "level-swept":
                # A high-priority liquidity level was crossed. Re-evaluate hypothesis
                # from a neutral state so it makes an unbiased direction assessment.
                # Without clearing first, hypothesis is sticky and won't change an
                # existing direction unless the evidence is overwhelming.
                _hyp_snap = _smt_state.load_hypothesis()
                _hyp_snap["direction"] = "none"
                _smt_state.save_hypothesis(_hyp_snap)
                _level_hyp_divs = _hyp_mod.run_hypothesis(
                    now, today_mnq, today_mes,
                    self._hist_mnq_1m, self._hist_mes_1m,
                    hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
                    skip_position_reset=True,
                )
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
                    _pos = _smt_state.load_position()
                    _pos["confirmation_bar"] = {}
                    _pos["limit_entry"] = ""
                    _smt_state.save_position(_pos)
                    _tb_sig: dict = {
                        "kind":             "trend-broken",
                        "time":             trend_sig["time"],
                        "price":            trend_sig["price"],
                        "broken_direction": _hyp_dir,
                        "level_name":       trend_sig.get("level_name", ""),
                        "level_price":      trend_sig.get("level_price", ""),
                        "direction":        _new_dir,
                    }
                    for _k in ("bar_low", "bar_high"):
                        if _k in trend_sig:
                            _tb_sig[_k] = trend_sig[_k]
                    self._emit(_tb_sig)
                    events.append(_tb_sig)
                    if _prev_limit != "":
                        _cancel_sig = {
                            "kind":      "limit-entry-cancelled",
                            "time":      now.isoformat(),
                            "price":     float(_prev_limit),
                            "reason":    "trend-broken",
                            "direction": _hyp_dir,
                        }
                        self._emit(_cancel_sig)
                        events.append(_cancel_sig)
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
                    self._emit(_d)
                    events.append(_d)
                _hyp_dir = _new_dir
            elif trend_sig["kind"] == "ath-crossed":
                # Bar straddled the fixed session_ath (09:20 ATH) with direction not
                # "down". First time into uncharted territory — emit trend-broken,
                # re-run hypothesis (will form with direction="down"), cancel limits.
                _ath_hyp_divs = _hyp_mod.run_hypothesis(
                    now, today_mnq, today_mes,
                    self._hist_mnq_1m, self._hist_mes_1m,
                    hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
                    skip_position_reset=True,
                )
                _new_dir = _smt_state.load_hypothesis().get("direction", "none")
                _tb_sig = {
                    "kind":             "trend-broken",
                    "time":             trend_sig["time"],
                    "price":            trend_sig["price"],
                    "broken_direction": _hyp_dir,
                    "level_name":       "ath",
                    "level_price":      trend_sig.get("all_time_high", ""),
                    "direction":        _new_dir,
                    "bar_high":         trend_sig.get("bar_high", ""),
                    "bar_low":          trend_sig.get("bar_low", ""),
                }
                self._emit(_tb_sig)
                events.append(_tb_sig)
                if _prev_limit != "":
                    _cancel_sig = {
                        "kind":      "limit-entry-cancelled",
                        "time":      now.isoformat(),
                        "price":     float(_prev_limit),
                        "reason":    "trend-broken",
                        "direction": _hyp_dir,
                    }
                    self._emit(_cancel_sig)
                    events.append(_cancel_sig)
                for _d in (_ath_hyp_divs or []):
                    self._emit(_d)
                    if _d.get("kind") == "new-hypothesis":
                        self._last_hyp_cautious = (
                            _d.get("cautious_price_initial", ""),
                            _d.get("cautious_price_secondary", ""),
                        )
                events.extend(_ath_hyp_divs or [])
                _hyp_dir = _new_dir
            elif trend_sig["kind"] == "dynamic-ath-crossed":
                # Already above session_ath with direction="down". The running high
                # was straddled — cautious prices may have shifted. Re-run hypothesis
                # and emit new-hypothesis ONLY if cautious prices changed. No trend-broken.
                _dath_hyp_divs = _hyp_mod.run_hypothesis(
                    now, today_mnq, today_mes,
                    self._hist_mnq_1m, self._hist_mes_1m,
                    hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
                    skip_position_reset=True,
                )
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
                if _prev_limit != "" and _smt_state.load_position().get("limit_entry", "") == "":
                    _cancel_sig = {
                        "kind":      "limit-entry-cancelled",
                        "time":      now.isoformat(),
                        "price":     float(_prev_limit),
                        "reason":    trend_sig.get("kind", "trend-broken"),
                        "direction": _hyp_dir,
                    }
                    self._emit(_cancel_sig)
                    events.append(_cancel_sig)

        is_5m = (now.minute % 5 == 0)

        if is_5m:
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
                    self._emit(d)
                    events.append(d)
            # Reload direction so strategy sees the updated bias on the same bar.
            _hyp_dir = _smt_state.load_hypothesis().get("direction", "none")

        # Fix #1: run_strategy on every 1m bar; full entry logic only at 5m boundaries.
        strat_sig = _strat_mod.run_strategy(now, mnq_1m_bar, recent, fill_check_only=not is_5m)
        if strat_sig is not None:
            strat_sig.setdefault("direction", _hyp_dir)
            # Emit cancel when strategy's market-entry overwrites a pending limit that
            # was never explicitly cancelled by trend (trend cancel path handled above).
            if strat_sig["kind"] == "market-entry" and _prev_limit != "":
                _cancel_sig = {
                    "kind":      "limit-entry-cancelled",
                    "time":      now.isoformat(),
                    "price":     float(_prev_limit),
                    "reason":    "market-entry",
                    "direction": _hyp_dir,
                }
                self._emit(_cancel_sig)
                events.append(_cancel_sig)
            self._emit(strat_sig)
            events.append(strat_sig)

        return events
