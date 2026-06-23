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

# SMT V2 refinement #1: dedup level-SMTs whose underlying LEVEL prices are within this
# tolerance (MNQ points) of each other within one side+bar — a single move can take out
# several near-coincident levels (e.g. week_high ≈ day_high) and would otherwise emit a
# duplicate SMT per level. We keep only the highest-scope level per cluster.
DEDUP_TOL_PTS = 5.0

# Scope priority for dedup (higher = kept). Derived from the ref_name prefix.
_SCOPE_RANK = {"ath": 3, "week": 2, "day": 1, "session": 0}


def _bar_row_has_ohlc(bar_row, *fields: str) -> bool:
    """True iff bar_row exposes every named field with a non-NaN value.

    automation.main builds a degenerate empty ``pd.Series(dtype=float)`` for an
    instrument that has no partial-bar data for the current minute (e.g. a MES feed
    gap — see the 2026-06-10 MES outage). Scalar access like ``float(bar_row["High"])``
    would then raise ``KeyError``. Callers use this to skip the instrument's per-bar
    passes cleanly instead of crashing.

    Works for both a ``pd.Series`` (live path) and a plain ``dict`` (backtest path,
    backtest_smt.py): both raise KeyError on a missing key, so ``bar_row[_f]`` is the
    common accessor — do NOT use ``bar_row.index`` (a dict has no ``.index``).
    """
    for _f in fields:
        try:
            _v = bar_row[_f]
        except (KeyError, IndexError):
            return False
        if pd.isna(_v):
            return False
    return True


def _level_scope(ref_name: str) -> str:
    """Map a level ref_name to its scope bucket for dedup priority.

    ATH* → ath, week_* → week, day_* → day, everything else (the 6hr ny_morning /
    ny_evening / london / asia session levels) → session.
    """
    if ref_name.startswith("ATH"):
        return "ath"
    if ref_name.startswith("week_"):
        return "week"
    if ref_name.startswith("day_"):
        return "day"
    return "session"


def _dedup_level_smts(records: list[dict], lvl_px: dict[str, float]) -> list[dict]:
    """Dedup level (kind=='smt') records by scope within each side.

    Within a side, cluster level-SMT records whose underlying LEVEL prices
    (lvl_px[ref_name]) are within DEDUP_TOL_PTS of each other; keep only ONE per cluster
    — the highest-scope level (ATH > week > day > session), ties broken deterministically
    by ref_name. Records whose level price isn't in lvl_px are kept as-is. Fill records
    (kind!='smt') are exempt and passed through untouched. Deterministic ordering.
    """
    smt_recs = [r for r in records if r.get("kind") == "smt"]
    other = [r for r in records if r.get("kind") != "smt"]

    # Records we can't place a level price for are kept verbatim (shouldn't happen).
    keepable: list[dict] = []
    cluster_pool: list[dict] = []
    for r in smt_recs:
        if r.get("ref_name") in lvl_px and lvl_px[r["ref_name"]] is not None:
            cluster_pool.append(r)
        else:
            keepable.append(r)

    kept: list[dict] = list(keepable)
    # Group by side, then greedily cluster by level price within DEDUP_TOL_PTS.
    for side in sorted({r.get("side") for r in cluster_pool}):
        members = sorted(
            [r for r in cluster_pool if r.get("side") == side],
            key=lambda r: (lvl_px[r["ref_name"]], r["ref_name"]),
        )
        used = [False] * len(members)
        for i, r in enumerate(members):
            if used[i]:
                continue
            cluster = [r]
            used[i] = True
            base_px = lvl_px[r["ref_name"]]
            for j in range(i + 1, len(members)):
                if used[j]:
                    continue
                if abs(lvl_px[members[j]["ref_name"]] - base_px) <= DEDUP_TOL_PTS:
                    cluster.append(members[j])
                    used[j] = True
            # Keep the highest-scope member; tie-break deterministically by ref_name.
            winner = max(
                cluster,
                key=lambda m: (_SCOPE_RANK[_level_scope(m["ref_name"])], ),
            )
            # Stable tie-break: among same top rank, pick smallest ref_name.
            top_rank = _SCOPE_RANK[_level_scope(winner["ref_name"])]
            winner = min(
                [m for m in cluster if _SCOPE_RANK[_level_scope(m["ref_name"])] == top_rank],
                key=lambda m: m["ref_name"],
            )
            kept.append(winner)

    # Preserve original record ordering (deterministic), with fills appended after.
    kept_ids = {id(r) for r in kept}
    return [r for r in records if id(r) in kept_ids or r.get("kind") != "smt"]


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


def _detect_yesterday_session_fvgs(
    combined_1m: pd.DataFrame, now: pd.Timestamp,
) -> list[dict]:
    """ALL 1hr FVGs (3-bar imbalance) formed during YESTERDAY's CME session — regardless
    of whether they were later filled. Used as an additive fill universe so SMT-fills can
    fire against the prior session's FVGs through the current session.

    "Yesterday's session" = the CME session immediately before the current one. The
    current session opened at cme_session_start(now) (18:00 ET); yesterday's session ran
    from 24h before that (prior 18:00 ET) to 1h before it (17:00 ET on the current
    session-open day), matching the 18:00→17:00 ET CME session span.

    Returns entries shaped like daily._detect_fvgs output (name/kind/top/bottom) plus a
    `keep: True` flag so the per-bar visited-prune leaves them in place (the fill state
    machine handles single-fire + re-arm). FVG formation timestamp (the 3rd/completing
    1hr bar) must fall inside the yesterday-session window. No unvisited filter is applied.
    """
    if combined_1m is None or combined_1m.empty:
        return []
    _sess_open = pd.Timestamp(_cme_session_start(now))
    _y_start = _sess_open - pd.Timedelta(hours=24)  # prior day 18:00 ET
    _y_end = _sess_open - pd.Timedelta(hours=1)      # current session-open day 17:00 ET

    _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    # Resample over a window padded 2h on the left so an FVG whose 3rd bar lands at
    # _y_start still has its first two bars available.
    _src = combined_1m[
        (combined_1m.index >= _y_start - pd.Timedelta(hours=2))
        & (combined_1m.index < _y_end + pd.Timedelta(hours=1))
    ]
    if _src.empty:
        return []
    _hourly = _src.resample("1h", label="left").agg(_agg).dropna(subset=["Open"])
    if len(_hourly) < 3:
        return []

    highs = _hourly["High"].values
    lows = _hourly["Low"].values
    idx = _hourly.index

    # 1m closes from the yesterday-session window onward — the reference for the far-edge
    # invalidation filter below (bounded once; the per-FVG slice is a cheap index compare).
    _chk = combined_1m[combined_1m.index >= _y_start]
    _chk_idx = _chk.index
    _chk_close = _chk["Close"].values if not _chk.empty else None

    out: list[dict] = []
    seen: set[str] = set()
    for i in range(len(_hourly) - 2):
        bar1_h, bar1_l = highs[i], lows[i]
        bar3_h, bar3_l = highs[i + 2], lows[i + 2]
        if bar3_l > bar1_h:
            top, bottom, side = float(bar3_l), float(bar1_h), "bull"
        elif bar3_h < bar1_l:
            top, bottom, side = float(bar1_l), float(bar3_h), "bear"
        else:
            continue
        formation_ts = idx[i + 2]
        # Formation (completing 3rd bar) must fall in the yesterday-session window.
        if not (_y_start <= formation_ts <= _y_end):
            continue
        # Invalidation filter: drop a carried FVG once price has CLOSED decisively through its
        # FAR edge after formation (bullish → a close below the bottom; bearish → a close above
        # the top). Such a gap is consumed/failed and is no longer a valid imbalance — keeping it
        # resurrects a dead level as a fill target (e.g. 2026-05-28 carried fvg_20260527_0400_bull,
        # which 05-27 closed ~250pt below its bottom). This is distinct from the unvisited check
        # (mere re-entry of the zone): it tests a decisive break of the far edge, the structural
        # death of the imbalance. Bars at/before formation are excluded.
        if _chk_close is not None:
            _pos = _chk_idx.searchsorted(formation_ts, side="right")
            _post_close = _chk_close[_pos:]
            if len(_post_close) > 0:
                if side == "bull" and (_post_close < bottom).any():
                    continue
                if side == "bear" and (_post_close > top).any():
                    continue
        ts_str = formation_ts.strftime("%Y%m%d_%H%M")
        name = f"fvg_{ts_str}_{side}"
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "kind": "fvg",
            "top": top,
            "bottom": bottom,
            "keep": True,
        })
    return out


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
        # GIL-27: per-bar Timestamp.floor() cache. The same `now` is floored to the same
        # freq at several call sites within one on_1m_bar pass (1min ×2, 5min ×2, 1h across
        # MNQ+MES), and Timestamp.floor() is surprisingly costly (pytz localize + np.isclose
        # ~5% of 1s runtime). Memoize per bar: identity-reset on a new `now`, then reuse.
        # Returns exactly now.floor(freq), so output is byte-identical.
        self._floor_bar_ts: pd.Timestamp | None = None
        self._floor_bar_cache: dict[str, pd.Timestamp] = {}
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
        # Tracks the pause-sentinel state seen on the previous bar. A True→False flip is a
        # pause→resume transition: arm a force-eval (same machinery as the post-exit path) so
        # the strategy re-runs its FULL entry evaluation against the last completed 5m bar
        # immediately, rather than waiting for the next 5m boundary. None = not yet observed.
        self._prev_paused: bool | None = None
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
        # O2 (GIL-34): once-per-session guard for the 09:29 ET cautious-target recompute.
        self._last_0929_recompute_date: "datetime.date | None" = None
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
        # (high, low, close_high, close_low) — close_* feed body-SMT comparison levels.
        self._dyn_day_hl: "tuple[float | None, ...]" = (None, None, None, None)
        self._dyn_week_key: "pd.Timestamp | None" = None
        self._dyn_week_hl: "tuple[float | None, ...]" = (None, None, None, None)
        # Constant 18:00–session-open hist sliver (the ~5 pre-18:05 bars the asia session
        # window needs); concatenated with today_mnq to feed _session_bars a small frame.
        self._dyn_sliver18: "pd.DataFrame | None" = None

        # ── SMT V2: MES per-instrument liquidity + 1hr FVG frame (additive) ──────
        # Mirrors the MNQ dynamic-liquidity caches/frames above, written to the
        # additive `liquidities_mes` daily.json key. The MNQ caches/frames are
        # untouched (regression-sensitive).
        self._fvg_mes_1hr: pd.DataFrame | None = None
        self._fvg_done_mes_1hr: pd.Timestamp | None = None
        self._dyn_mes_hist_tail: "pd.DataFrame | None" = None
        self._dyn_mes_day_key: "pd.Timestamp | None" = None
        self._dyn_mes_day_hl: "tuple[float | None, ...]" = (None, None, None, None)
        self._dyn_mes_week_key: "pd.Timestamp | None" = None
        self._dyn_mes_week_hl: "tuple[float | None, ...]" = (None, None, None, None)
        self._dyn_mes_sliver18: "pd.DataFrame | None" = None

        # ── SMT V2: detection buffers + reference consumer + persisted state ─────
        from smt_detect import SmtBuffer, PendingSmtWatch
        self._smt_buffer = SmtBuffer()
        self._pending_watch = PendingSmtWatch()
        self._detect_state: dict = {}
        # Per-frame last-processed boundary guards for hidden-SMT 15m/30m resamples.
        self._hidden_done: dict[str, pd.Timestamp | None] = {"1min": None}
        # Count of invalidation events already flushed to smt_invalidations.json, so the
        # per-bar trail write only re-serializes when a NEW event was appended this bar.
        self._inv_written_n: int = 0

        # ── R3: 1m-cadence SMT detection (determinism across replay modes) ───────
        # SMT DETECTION must fire only on COMPLETED 1m bars, using the completed bar, so
        # that 1m regression, 1s regression, and live all produce the same smt-div stream.
        # ORDER EXECUTION stays at 1s cadence (handled outside the detection block).
        #
        # _last_detect_minute: the floor("1min") of the last minute whose COMPLETED bar was
        # fed to detection. In the per-second (1s/live) path, a minute is complete only once
        # `now` crosses into the next minute; the rollover then runs detection for the just-
        # completed minute using its finalized bar (reconstructed from today_mnq/today_mes).
        self._last_detect_minute: "pd.Timestamp | None" = None
        # Pre-detection daily.json snapshot captured at the START of the current detection
        # minute (before that minute's first per-second liquidity update). When the minute
        # rolls over, this snapshot reflects liquidity state through the PRIOR completed bar
        # — exactly the `_pre_daily` a 1m-mode call sees (refinement #2 one-bar lag). Keyed
        # by the minute it belongs to so a stale snapshot is never reused.
        self._detect_pre_daily: "dict | None" = None
        self._detect_pre_daily_minute: "pd.Timestamp | None" = None
        # 5m-bucket of the last completed-bar detection that the emit-gate let through. The
        # gate currently ignores this (always emits); it is the seam for a future "only when
        # the 5m bucket changed since the last emission" tightening (see _should_emit_smt).
        self._last_emitted_5m_bucket: "pd.Timestamp | None" = None
        # Detection-side 5m-boundary tracker — independent of _last_5m_processed (which the
        # execution-side hypothesis block owns). Detection runs on COMPLETED bars, so its
        # `is_5m` (drives the SMT buffer drain + 5m reference-consumer cadence) must key off
        # the completed detection minute, not the current intra-minute partial.
        self._last_detect_5m: "pd.Timestamp | None" = None

    def on_daily_or_startup(
        self, now: pd.Timestamp, today_mnq: pd.DataFrame,
        today_mes: "pd.DataFrame | None" = None,
    ) -> None:
        """Compute fixed reference liquidities and seed ATH. Called on startup and at 09:20 ET daily.

        `today_mes` (additive, SMT V2): when provided, seeds the parallel
        `liquidities_mes` block from `_hist_mes_1m` + today's MES bars. When None the
        MES levels are seeded from history alone (today contributes nothing yet)."""
        from smt_state import (
            DEFAULT_DAILY, DEFAULT_GLOBAL,
            load_daily, load_global, save_daily, save_global,
        )
        from hypothesis import compute_live_hl_mid
        from daily import _session_bars

        # Seed all_time_high (the running ATH) and session_ath (its session-open snapshot).
        # GIL-23: session_ath must be the PERSISTED true ATH, not the short windowed in-memory
        # IB frame max — on 2026-06-11 that windowed max collapsed to 29011.25 (vs the true
        # 30807) and silently disabled rule2b's recovery guard. all_time_high already persists
        # cross-session in general_live_dir()/global.json and is only ever raised when a new high
        # supersedes it, so we DERIVE session_ath from it rather than re-initialising it from the
        # volatile hist window each session. No-op in backtest (in-memory): there all_time_high
        # resolves to max(0, 60-day-window max) = the same value the old windowed seed produced.
        _global = load_global()
        if not self._hist_mnq_1m.empty:
            _hist_ath = float(self._hist_mnq_1m["High"].max())
            _persisted = float(_global.get("all_time_high", 0.0))
            # Corruption guard (GIL-23 follow-up): a persisted ATH implausibly far above the
            # (back-adjusted) data max — a manual sentinel like 999999, or a stale pre-rollover
            # contract scale — must NOT stick via max(). A legitimate ATH is always a bar inside
            # the hist window, so it never exceeds ~1x _hist_ath; >3x means corruption → re-anchor
            # to the data max. (A too-LOW stale value self-heals via the max() below.)
            if _hist_ath > 0 and _persisted > _hist_ath * 3:
                _persisted = 0.0
            _global["all_time_high"] = max(_persisted, _hist_ath)
            _global["session_ath"]   = _global["all_time_high"]
            self._session_ath = _global["session_ath"]
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

        # SMT V2: seed the MES rolling 1hr FVG frame (hist + today's COMPLETED bars).
        _combined_mes = pd.concat(
            [self._hist_mes_1m, today_mes if today_mes is not None else self._hist_mes_1m.iloc[:0]]
        ).sort_index()
        _combined_mes = _combined_mes[~_combined_mes.index.duplicated(keep="last")]
        if not _combined_mes.empty:
            _fvg_mes_full = (
                _combined_mes.resample("1h", label="left").agg(_agg).dropna(subset=["Open"])
            )
            self._fvg_mes_1hr = _fvg_mes_full[
                (_fvg_mes_full.index >= _14d_ago) & (_fvg_mes_full.index < now.floor("1h"))
            ]
        else:
            self._fvg_mes_1hr = pd.DataFrame(columns=list(_agg))
        self._fvg_done_mes_1hr = None
        # Reset per-session MES dynamic caches so a 09:20 re-run recomputes them.
        self._dyn_mes_hist_tail = None
        self._dyn_mes_day_key = None
        self._dyn_mes_day_hl = (None, None, None, None)
        self._dyn_mes_week_key = None
        self._dyn_mes_week_hl = (None, None, None, None)
        self._dyn_mes_sliver18 = None

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
        # Body-extreme (CLOSE-based) seed for day/week high/low — the same window as the
        # wick H/L but using Close max/min (no outlier skip). The per-bar dynamic pass
        # overwrites these immediately; this just seeds a consistent initial close_price.
        _dw_close = self._seed_close_extremes(_combined, _now_ts)
        for _name in ("week_high", "week_low", "week_mid", "day_high", "day_low", "day_mid"):
            if _name in _live:
                _existing = next((l for l in _liq if l["name"] == _name), None)
                if _existing:
                    _existing["price"] = _live[_name]
                    if _name in _dw_close:
                        _existing["close_price"] = _dw_close[_name]
                else:
                    _entry = {"name": _name, "kind": "level", "price": _live[_name]}
                    if _name in _dw_close:
                        _entry["close_price"] = _dw_close[_name]
                    _liq.append(_entry)
        # Seed session highs/lows from completed sessions in combined bars.
        _today = now.date()
        for _sess in ("asia", "london", "ny_morning", "ny_evening"):
            _sbars = _session_bars(_combined, _sess, _today)
            if not _sbars.empty:
                for _suffix, _col in (("high", "High"), ("low", "Low")):
                    _key = f"{_sess}_{_suffix}"
                    _price = float(_sbars[_col].max() if _suffix == "high" else _sbars[_col].min())
                    _cprice = float(_sbars["Close"].max() if _suffix == "high" else _sbars["Close"].min())
                    _ex = next((l for l in _liq if l["name"] == _key), None)
                    if _ex:
                        _ex["price"] = _price
                        _ex["close_price"] = _cprice
                    else:
                        _liq.append({"name": _key, "kind": "level", "price": _price, "close_price": _cprice})
        # SMT-fills universe (B): ALL 1hr FVGs formed during yesterday's CME session,
        # regardless of later fill. Added with keep:True so the per-bar prune leaves them
        # as fill targets through the session. MNQ side.
        _existing_mnq_fvg = {l["name"] for l in _liq}
        for _yf in _detect_yesterday_session_fvgs(_combined, now):
            if _yf["name"] not in _existing_mnq_fvg:
                _liq.append(_yf)
                _existing_mnq_fvg.add(_yf["name"])
            else:
                # Symmetric with the MES side: upgrade an already-added (unvisited, no-keep)
                # FVG to a keep fill target so the visited-prune leaves it in place.
                _ex = next((l for l in _liq if l.get("name") == _yf["name"]), None)
                if _ex is not None:
                    _ex["keep"] = True
        _state["liquidities"] = _liq
        # Universe (B): prev-day (14 trading days) + prev-week (2 Mon–Fri weeks) extremes
        # as FIXED SMT levels, in an ADDITIVE key consumed only by SMT detection — kept
        # OUT of `liquidities` so the strategy's failed-entry sweep (_ext_levels) is
        # byte-identical and trades are unchanged.
        _state["liquidities_universe"] = _daily_mod.compute_universe_levels(_combined, now.date())
        save_daily(_state)

        # ── SMT V2: seed parallel MES day/week/session levels + 1hr FVGs ─────────
        # Mirrors the MNQ seed above against the MES combined frame, writing the
        # additive `liquidities_mes` block. Never touches the MNQ `liquidities` key.
        from daily import _detect_fvgs
        _state = load_daily()
        _liq_mes = _state.get("liquidities_mes", [])
        if not _combined_mes.empty:
            _live_mes = compute_live_hl_mid(_combined_mes, _now_ts)
            _dw_close_mes = self._seed_close_extremes(_combined_mes, _now_ts)
            for _name in ("week_high", "week_low", "week_mid", "day_high", "day_low", "day_mid"):
                if _name in _live_mes:
                    _ex = next((l for l in _liq_mes if l["name"] == _name), None)
                    if _ex:
                        _ex["price"] = _live_mes[_name]
                        if _name in _dw_close_mes:
                            _ex["close_price"] = _dw_close_mes[_name]
                    else:
                        _entry = {"name": _name, "kind": "level", "price": _live_mes[_name]}
                        if _name in _dw_close_mes:
                            _entry["close_price"] = _dw_close_mes[_name]
                        _liq_mes.append(_entry)
            for _sess in ("asia", "london", "ny_morning", "ny_evening"):
                _sbars = _session_bars(_combined_mes, _sess, _today)
                if not _sbars.empty:
                    for _suffix, _col in (("high", "High"), ("low", "Low")):
                        _key = f"{_sess}_{_suffix}"
                        _price = float(_sbars[_col].max() if _suffix == "high" else _sbars[_col].min())
                        _cprice = float(_sbars["Close"].max() if _suffix == "high" else _sbars["Close"].min())
                        _ex = next((l for l in _liq_mes if l["name"] == _key), None)
                        if _ex:
                            _ex["price"] = _price
                            _ex["close_price"] = _cprice
                        else:
                            _liq_mes.append({"name": _key, "kind": "level", "price": _price, "close_price": _cprice})
            # 1hr FVGs from the seeded MES frame (visited check against MES combined 1m).
            _existing_mes = {l["name"] for l in _liq_mes}
            for _f in _detect_fvgs(self._fvg_mes_1hr, _combined_mes):
                if _f["name"] not in _existing_mes:
                    _liq_mes.append(_f)
                    _existing_mes.add(_f["name"])
            # SMT-fills universe (B): yesterday-session 1hr FVGs for MES (keep:True), so
            # _pair_fvgs finds the same yesterday FVGs on both legs by name.
            for _yf in _detect_yesterday_session_fvgs(_combined_mes, now):
                if _yf["name"] not in _existing_mes:
                    _liq_mes.append(_yf)
                    _existing_mes.add(_yf["name"])
                else:
                    # Already added by the unvisited _detect_fvgs pass above WITHOUT keep:True.
                    # Upgrade it to a keep fill target so the per-bar visited-prune leaves it
                    # in place — otherwise MES loses this leg once it fills the FVG while the
                    # MNQ leg survives (keep:True), so _pair_fvgs can no longer pair it and the
                    # fill SMT is missed (e.g. fvg_20260611_1600 fill_b @ 2026-06-12 03:03).
                    _ex = next((l for l in _liq_mes if l.get("name") == _yf["name"]), None)
                    if _ex is not None:
                        _ex["keep"] = True
        _state["liquidities_mes"] = _liq_mes
        # Universe (B): MES counterpart prev-day/prev-week extremes (additive key) so the
        # intersection in _detect_level_smts sees the same universe names on both legs.
        if not _combined_mes.empty:
            _state["liquidities_universe_mes"] = _daily_mod.compute_universe_levels(
                _combined_mes, now.date())
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
                "liquidities_universe": _daily_state.get("liquidities_universe", []),
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

        # SMT V2: restore persisted edge/re-arm state + the reference consumer's
        # retained set (live restart continuity). The in-memory (backtest) store
        # returns DEFAULT_SMTS on a fresh run, so this is a clean reset there.
        from smt_detect import PendingSmtWatch as _PendingSmtWatch
        _smts = _smt_state.load_smts()
        self._detect_state = _smts.get("detect_state", {}) or {}
        self._pending_watch = _PendingSmtWatch.from_dict(_smts.get("watch", {}))
        # Cold-start guard for the pre-startup warm-up below: a genuine first start of this
        # session has no restored per-level detect_state (ignoring the reserved bookkeeping
        # keys). A warm restart already reflects the pre-startup bars in detect_state, so we
        # must NOT replay them again (would double-fire). Snapshot emptiness BEFORE the
        # warm-up mutates detect_state.
        _cold_start = not any(
            not str(_k).startswith("__") for _k in self._detect_state.keys()
        )

        # Always run the daily/startup liquidity computation.
        self.on_daily_or_startup(now, today_mnq_at_open)
        self._daily_triggered = True

        # Pre-startup SMT warm-up: if the orchestrator started LATE (after the 18:00 ET
        # session open) on a cold start, replay [session_open, now) through the SMT detector
        # so SMTs that fired before startup are detected, logged (source="v2-warmup"), and
        # folded into detect_state / smts.json — matching what the live loop would have built
        # had it been running. Runs AFTER liquidities are seeded and BEFORE the first
        # hypothesis. No hypothesis-direction wiring (scope: detection + logging only).
        if _cold_start:
            # R2 Phase 1.1 (GIL-25): seed per-ticker depletion for PAST liquidities (prev-day/
            # week) already run beyond before this session opened, so the warm-up + live loop
            # skip them instead of firing spurious mid-session SMTs (and the cautious/hypothesis
            # fan-out drops them as targets from bar 0). Runs BEFORE the warm-up replay.
            self._seed_pre_session_invalidation(now)
            self._warmup_replay_smts(now, today_mnq_at_open)

        # Reset hypothesis and position only when explicitly forced.
        if force_reset:
            save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))
            save_position(copy.deepcopy(DEFAULT_POSITION))
            # GIL-25 Phase 1.2: carry forward still-valid SMTs from prior sessions. Must run
            # AFTER the force_reset save (which overwrites smt_active_set with the default empty
            # list) and BEFORE run_hypothesis (so `divs` sees the survivors). Cold-start only.
            # Re-validates against the overnight gap; drops taken-out/aged/duplicate carried
            # SMTs. Shadow-only (no direction wiring).
            if _cold_start:
                self._ingest_pending_smts(now, today_mnq_at_open)
                # GIL-39 Change A: seed standing conviction from the just-merged carried
                # survivors so the rule2b override can challenge direction at the open.
                # No-op when the flag is OFF.
                import smt_conviction as _smt_conv
                _hyp_seed = _smt_state.load_hypothesis()
                _survivors = _hyp_seed.get("smt_active_set", []) or []
                _seeded = _smt_conv.seed_from_standing(
                    _hyp_seed.get("smt_conviction_set", []) or [], _survivors, now.isoformat()
                )
                _hyp_seed["smt_conviction_set"] = _seeded
                _smt_state.save_hypothesis(_hyp_seed)
            # Run first hypothesis so direction is populated immediately after force-reset.
            _init_hyp_divs = _hyp_mod.run_hypothesis(
                now, today_mnq_at_open, self._hist_mes_1m,
                self._hist_mnq_1m, self._hist_mes_1m,
                hist_1hr=self._hist_1hr, hist_4hr=self._hist_4hr,
            )
            for _d in (_init_hyp_divs or []):
                self._emit(_d)
            return

        # GIL-25 Phase 1.2 (no-force_reset path): carry still-valid SMTs forward, MERGING into
        # the warm-up's smt_active_set writes (not overwritten here). Cold-start only; BEFORE
        # run_hypothesis so `divs` sees the survivors. Shadow-only (no direction wiring).
        if _cold_start:
            self._ingest_pending_smts(now, today_mnq_at_open)
            # GIL-39 Change A: seed standing conviction from the just-merged carried survivors
            # so the rule2b override can challenge direction at the open. No-op when flag OFF.
            import smt_conviction as _smt_conv
            _hyp_seed = _smt_state.load_hypothesis()
            _survivors = _hyp_seed.get("smt_active_set", []) or []
            _seeded = _smt_conv.seed_from_standing(
                _hyp_seed.get("smt_conviction_set", []) or [], _survivors, now.isoformat()
            )
            _hyp_seed["smt_conviction_set"] = _seeded
            _smt_state.save_hypothesis(_hyp_seed)

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
            _new_hyp_dir = _smt_state.load_hypothesis_ro().get("direction", "none")

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

        # Late-startup analogue of the resume transition: if the strategy comes up flat (no
        # active position, no resting entry) and is NOT paused at startup, arm a force-eval so
        # the first live bar evaluates the FULL entry against the last completed 5m bar instead
        # of waiting for the next 5m boundary. Seed _prev_paused from the current sentinel so the
        # per-bar transition detector has a correct baseline (a startup while paused is not, by
        # itself, a resume; the True→False flip is detected on the later un-pause).
        _start_paused = _smt_state.is_paused()
        self._prev_paused = _start_paused
        if not _start_paused and not _has_active:
            _start_pos = load_position()
            if _start_pos.get("stop_entry", "") == "":
                self._force_entry_eval_after = now

    def _floor(self, now: pd.Timestamp, freq: str) -> pd.Timestamp:
        """now.floor(freq), memoized for the current bar. Identity-reset when `now`
        changes, so within one on_1m_bar pass every call site sharing the same `now`
        recomputes the floor at most once. Byte-identical to a direct now.floor(freq).

        Self-initializing so it stays correct on instances built via __new__ that bypass
        __init__ (e.g. the _extend_fvg_frames unit tests), matching the old now.floor()'s
        lack of any instance-state precondition."""
        cache = getattr(self, "_floor_bar_cache", None)
        if cache is None or now is not self._floor_bar_ts:
            self._floor_bar_ts = now
            self._floor_bar_cache = cache = {}
        v = cache.get(freq)
        if v is None:
            # GIL-27: Timestamp.floor() is dominated by the pytz localize/np.isclose path
            # (~5× the cost). For sub-hour minute boundaries the floor is a pure wall-clock
            # truncation that DST never perturbs (DST shifts are whole hours), so .replace()
            # is byte-identical AND ~5.8× faster — verified equal across 82.8k timestamps on a
            # normal day and the DST spring-forward day. 1h/4h KEEP .floor() (the DST fall-back
            # ambiguous hour makes wall-clock-replace unsafe for hour-grained floors).
            if freq == "1min":
                v = now.replace(second=0, microsecond=0, nanosecond=0)
            elif freq == "5min":
                v = now.replace(minute=(now.minute // 5) * 5,
                                second=0, microsecond=0, nanosecond=0)
            else:
                v = now.floor(freq)
            cache[freq] = v
        return v

    def _recompute_cautious_at_0929(self, price: float, now=None) -> None:
        """O2 (GIL-34): re-anchor the cautious TARGETS to `price` (the 09:29 ET close),
        keeping DIRECTION unchanged, so the 09:30-09:45 take-profit-on-touch and any
        position rolling into the open trade against fresh, reachable levels.

        Two updates: (1) the live hypothesis ladder, anchored at `price` for ITS OWN
        direction (no-op if direction is none / manual lock — recompute_cautious_for_fill);
        (2) if a position is open, the FROZEN ladder is re-anchored against the position's
        mgmt_direction (the trade is managed off the frozen snapshot, whose direction may
        differ from the live hypothesis).

        Protection guard (open position only): the INITIAL target governs the protective
        break-even / trailing-stop arming, so on an open position it is left FROZEN entirely —
        re-anchoring it (even tighter) perturbs the arm chain and can turn a managed win into a
        raw stop-out (observed 06-16, where a sub-point initial shift broke the +$52 protective
        exit). Only the SECONDARY target — the level the 09:30-09:45 take-profit-on-touch acts
        on — is re-anchored on an open position, and then only when the new level is CLOSER to
        `price` (tighten-only); a farther secondary is kept. The live (flat) hypothesis is fully
        re-anchored (no open trade to protect). Best-effort; never raises into the bar loop."""
        try:
            dly = _smt_state.load_daily() or {}
            liq = dly.get("liquidities", [])
            ath = (_smt_state.load_global() or {}).get("all_time_high")
            # (1) Live hypothesis ladder — own direction (no open position to protect → full re-anchor).
            hyp = _smt_state.load_hypothesis()
            _hyp_mod.recompute_cautious_for_fill(hyp, price, liq, ath, now=now)
            _smt_state.save_hypothesis(hyp)
            # (2) Open position — re-anchor the frozen ladder against mgmt_direction, tighten-only.
            pos = _smt_state.load_position()
            active = pos.get("active") or {}
            mgmt_dir = active.get("mgmt_direction")
            if active and mgmt_dir in ("up", "down"):
                _mgmt_hyp = {"direction": mgmt_dir}
                _hyp_mod.recompute_cautious_for_fill(
                    _mgmt_hyp, price, liq, ath, pos.get("cautious_dist_shrinks", 0), now=now)

                def _to_f(x):
                    try:
                        return float(x) if x not in ("", None) else None
                    except (TypeError, ValueError):
                        return None

                def _tighter(new_raw, new_lvl, old_raw, old_lvl):
                    """Keep whichever target is closer to `price` (more protective). Empty new →
                    keep old; empty old → take new."""
                    nv, ov = _to_f(new_raw), _to_f(old_raw)
                    if nv is None:
                        return old_raw, old_lvl
                    if ov is None:
                        return new_raw, new_lvl
                    return (new_raw, new_lvl) if abs(nv - price) <= abs(ov - price) \
                        else (old_raw, old_lvl)

                # INITIAL: frozen — the protective layer is never re-anchored on an open trade.
                ci_raw, ci_lvl = active.get("cautious_initial", ""), active.get("cautious_initial_level", "")
                # SECONDARY: re-anchored tighten-only (the take-profit-on-touch target).
                cs_raw, cs_lvl = _tighter(
                    _mgmt_hyp.get("cautious_price_secondary", ""),
                    _mgmt_hyp.get("cautious_price_secondary_level", ""),
                    active.get("cautious_secondary", ""),
                    active.get("cautious_secondary_level", ""))
                _chosen = {
                    "direction": mgmt_dir,
                    "cautious_price_initial":         ci_raw,
                    "cautious_price_initial_level":   ci_lvl,
                    "cautious_price_secondary":       cs_raw,
                    "cautious_price_secondary_level": cs_lvl,
                }
                _smt_state.freeze_active_mgmt(active, mgmt_dir, _chosen)
                pos["active"] = active
                _smt_state.save_position(pos)
        except Exception:
            pass

    def on_1m_bar(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,
        mes_bar_row: pd.Series,
        today_mnq: pd.DataFrame,
        today_mes: pd.DataFrame,
        bar_complete: "bool | None" = None,
    ) -> list[dict]:
        """Process one bar. Returns list of emitted event dicts.

        `bar_complete` (R3): whether this call delivers a COMPLETED 1m bar (the bar_row is
        final OHLC for `now`'s minute) or an intra-minute partial.
          - True  → 1m-regression / completed-bar driver: SMT detection runs immediately on
                    this bar.
          - False → per-second 1s / live driver: this is a partial; SMT detection runs only
                    when the minute rolls over, on the just-completed prior minute's bar.
          - None  → auto-detect from cadence (see _run_completed_bar_detection): used by the
                    legacy unit tests that call on_1m_bar once per distinct minute.
        ORDER EXECUTION (trend / hypothesis / strategy below) is UNCHANGED — it runs on
        every call regardless of `bar_complete` (1s-cadence fidelity preserved)."""
        if not self._daily_triggered:
            return []

        # Re-run daily level computation at two transitions per CME session day.
        # 00:00 ET (London session start): today's midnight open is now available as TDO,
        #   replacing the prior-day proxy used during the Asia session.
        # 09:20 ET (NY pre-market): FVGs and session H/L are stable for the RTH session.
        # Only one fires per calendar date — whichever comes first sets _last_daily_date.
        _bar_floor = self._floor(now, "1min")
        _is_midnight = (now.hour == 0 and now.minute == 0)
        _is_0920    = (now.hour == 9 and now.minute == 20)
        if ((_is_midnight or _is_0920)
                and _bar_floor != self._last_daily_minute
                and now.date() != self._last_daily_date):
            self._last_daily_minute = _bar_floor
            self.on_daily_or_startup(now, today_mnq, today_mes)

        # O2 (GIL-34): at 09:29 ET, recompute the cautious TARGETS (not direction)
        # anchored at the current price, so the 09:30-09:45 take-profit-on-touch and any
        # position rolling into the open trade against fresh, reachable levels. Once per
        # session. Gated behind the O2 flag (default OFF → never fires → byte-identical).
        if (_trend_mod.SECONDARY_TP_ON_TOUCH_0930
                and now.hour == 9 and now.minute == 29
                and now.date() != self._last_0929_recompute_date):
            self._last_0929_recompute_date = now.date()
            self._recompute_cautious_at_0929(float(mnq_bar_row["Close"]), now=now)

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
        _hyp_dir = _smt_state.load_hypothesis_ro().get("direction", "none")

        # Snapshot stop_entry before trend runs so we can detect silent cancellations.
        _prev_stop = _smt_state.load_position_ro().get("stop_entry", "")

        # Pause→resume transition: when the pause sentinel flips True→False between bars, the
        # user is taking over from this moment — INCLUDING the just-closed 5m setup bar. Arm a
        # force-eval (same machinery as the post-exit path) so the strategy re-runs its FULL
        # entry evaluation against the last completed 5m bar on THIS bar, arming the stop-entry
        # iff the existing confirmation criteria are met — rather than waiting for the next 5m
        # boundary. Only when FLAT and with no entry already resting (the same preconditions the
        # normal entry path requires); never disturb an open position or an already-armed entry.
        _now_paused = _smt_state.is_paused()
        if (
            self._prev_paused is True
            and not _now_paused
            and self._force_entry_eval_after is None
        ):
            _resume_pos = _smt_state.load_position()
            if not _resume_pos.get("active") and _resume_pos.get("stop_entry", "") == "":
                self._force_entry_eval_after = now
        self._prev_paused = _now_paused

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
                if _smt_state.load_hypothesis_ro().get("manual"):
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
                    _new_dir = _smt_state.load_hypothesis_ro().get("direction", "none")
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
                _new_dir = _smt_state.load_hypothesis_ro().get("direction", "none")
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
                _hyp_dir = _smt_state.load_hypothesis_ro().get("direction", "none")
            else:
                # Normal trend signal (daily/weekly mid invalidation, or cautious exit).
                trend_sig.setdefault("direction", _hyp_dir)
                self._emit(trend_sig)
                events.append(trend_sig)
                # Emit a dedicated cancel signal if trend cleared a pending limit without one.
                if _prev_stop != "" and _smt_state.load_position_ro().get("stop_entry", "") == "":
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

        # SMT V2 refinement #2: snapshot daily.json BEFORE the per-bar liquidity update so
        # the detector evaluates against the PRIOR-bar extremes (a "touch" then means the
        # wick genuinely EXCEEDED the prior extreme — a real take-out — not merely equalled
        # the just-updated running extreme, which the leader would trivially touch).
        _pre_daily = _smt_state.load_daily_ro()  # GIL-27: read-only snapshot (fed to detection only)

        # R3: SMT DETECTION on COMPLETED 1m bars only (deterministic across replay modes).
        # Run the completed-bar detection driver BEFORE this call's per-second liquidity
        # update: in the per-second (1s/live) path a minute rollover here detects the
        # just-completed PRIOR minute against the daily snapshot taken at the start of that
        # minute (state through the bar before it) — matching the `_pre_daily` a 1m-mode
        # detection sees. `_pre_daily` (this call's pre-update snapshot) is also recorded as
        # the per-minute snapshot for the current minute. ORDER EXECUTION (trend / hypothesis
        # / strategy) stays at 1s cadence further below.
        _det_events = self._run_completed_bar_detection(
            now, mnq_bar_row, mes_bar_row, today_mnq, today_mes,
            pre_daily=_pre_daily, bar_complete=bar_complete,
        )
        for _sd in _det_events:
            self._emit(_sd)
            events.append(_sd)

        # Per-bar: update session/day/week H/L and prune visited FVGs. Runs every 1s so
        # EXECUTION (entry/fill/exit below) sees up-to-the-second liquidity state.
        self._update_dynamic_liquidities(now, mnq_bar_row, today_mnq)
        # SMT V2: update the parallel MES liquidities block (additive; never touches MNQ).
        self._update_mes_liquidities(now, mes_bar_row, today_mes)

        _this_5m = self._floor(now, "5min")
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
            _new_5m_dir = _smt_state.load_hypothesis_ro().get("direction", "none")
            if _new_5m_dir != _hyp_dir:
                self._accepted_level_sweeps.clear()
                self._swept_levels_since_hyp.clear()
            _hyp_dir = _new_5m_dir

        # GIL-42 (live-only, backtest-inert): if the close-reconcile ADOPTED a live broker
        # position (a phantom close), DISARM the pending re-entry force-eval so we don't
        # spuriously re-evaluate an entry after the adopted position later closes. ADOPT
        # already restored active (so run_strategy won't open a NEW entry while a position is
        # held — the primary suppression), this just clears the stale arming. Byte-identical
        # offline: backtest never writes recon_suppress_force_entry (only the live
        # apply_correction does); the read is a cheap no-op key check.
        if _smt_state.load_position_ro().get("recon_suppress_force_entry"):
            self._force_entry_eval_after = None
            self._force_market_entry = False
            _p = _smt_state.load_position()
            _p.pop("recon_suppress_force_entry", None)
            _smt_state.save_position(_p)

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
        if _smt_state.is_paused() and not _smt_state.load_position_ro().get("active"):
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
                    _sbsc_pos["cautious_dist_shrinks"] = _sbsc_pos.get("cautious_dist_shrinks", 0) + 1
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
        """Update MNQ session/day/week H/L and FVG state in daily.json on every bar.

        Thin wrapper over the instrument-generic helper, bound to the MNQ frame, the
        `liquidities` key, and the MNQ dynamic caches / FVG frames. Behavior is
        byte-identical to the pre-refactor single-instrument implementation
        (regression-guarded by test_mnq_liquidities_unchanged_regression).
        """
        return self._update_instrument_liquidities(
            now, mnq_bar_row, today_mnq,
            hist_1m=self._hist_mnq_1m,
            liq_key="liquidities",
            dyn_attrs=("_dyn_hist_tail", "_dyn_day_key", "_dyn_day_hl",
                       "_dyn_week_key", "_dyn_week_hl", "_dyn_sliver18"),
            fvg_specs=(("1h", "_fvg_1hr", "_fvg_done_1hr"),
                       ("4h", "_fvg_4hr", "_fvg_done_4hr")),
        )

    def _update_mes_liquidities(
        self,
        now: pd.Timestamp,
        mes_bar_row: pd.Series,
        today_mes: pd.DataFrame,
    ) -> list[dict]:
        """SMT V2: MES counterpart of _update_dynamic_liquidities, writing the additive
        `liquidities_mes` daily.json key. Mirrors the MNQ pass exactly but only the 1hr
        FVG frame (no 4hr — fill detection only needs 1hr)."""
        return self._update_instrument_liquidities(
            now, mes_bar_row, today_mes,
            hist_1m=self._hist_mes_1m,
            liq_key="liquidities_mes",
            dyn_attrs=("_dyn_mes_hist_tail", "_dyn_mes_day_key", "_dyn_mes_day_hl",
                       "_dyn_mes_week_key", "_dyn_mes_week_hl", "_dyn_mes_sliver18"),
            fvg_specs=(("1h", "_fvg_mes_1hr", "_fvg_done_mes_1hr"),),
        )

    def _update_instrument_liquidities(
        self,
        now: pd.Timestamp,
        bar_row: pd.Series,
        today_df: pd.DataFrame,
        *,
        hist_1m: pd.DataFrame,
        liq_key: str,
        dyn_attrs: tuple,
        fvg_specs: tuple,
    ) -> list[dict]:
        """Instrument-generic session/day/week H/L + FVG-prune + live-FVG pass.

        `dyn_attrs` = (hist_tail, day_key, day_hl, week_key, week_hl, sliver18) attribute
        names holding this instrument's per-session caches. `fvg_specs` = the
        (freq, frame_attr, done_attr) tuples for _extend_instrument_fvg_frames.
        """
        from daily import _session_bars, TIME_WINDOWS

        _tail_attr, _daykey_attr, _dayhl_attr, _weekkey_attr, _weekhl_attr, _sliver_attr = dyn_attrs

        _state = _smt_state.load_daily()
        _liq = _state.get(liq_key, [])
        _liq_map = {l["name"]: l for l in _liq}

        # An instrument with no partial-bar data this minute arrives as a degenerate empty
        # Series (automation.main builds pd.Series(dtype=float) when mes_partial is None).
        # Skip its liquidity pass rather than KeyError on bar_row["High"].
        if not _bar_row_has_ohlc(bar_row, "High", "Low"):
            return []

        _bar_high = float(bar_row["High"])
        _bar_low  = float(bar_row["Low"])
        _changed: list[str] = []
        _liq_events: list[dict] = []

        # hist_1m is constant per session and strictly precedes today_df, so cache an
        # 8-day tail once (covers the widest lookback: week H/L ≤5d, day H/L to 06:00).
        if getattr(self, _tail_attr) is None:
            _tail_cut = _cme_session_start(now) - pd.Timedelta(days=8)
            setattr(self, _tail_attr, hist_1m[hist_1m.index >= _tail_cut])
        _tail = getattr(self, _tail_attr)

        today_mnq = today_df
        # today_df rows are all <= now (last is the running partial bar), so its plain
        # max/min IS the [session_start, now] window extreme — shared by day + week below.
        _th_vals = today_mnq["High"].values
        _tl_vals = today_mnq["Low"].values
        _today_hi = float(_th_vals.max()) if len(_th_vals) else None
        _today_lo = float(_tl_vals.min()) if len(_tl_vals) else None
        # CLOSE-based extremes over the same today window (body SMT comparison level):
        # highest Close for *_high, lowest Close for *_low.
        _tc_vals = today_mnq["Close"].values
        _today_chi = float(_tc_vals.max()) if len(_tc_vals) else None
        _today_clo = float(_tc_vals.min()) if len(_tc_vals) else None

        # Helper: update a named level, track change. `close_price` (optional) is the
        # CLOSE-based extreme over the same window — stored alongside the wick `price` for
        # *_high/*_low levels so hidden (body) SMTs compare against the body extreme. It
        # never affects the `price` (wick) value or the change-detection (which keys on
        # `price`); it is updated whenever `price` is.
        def _set(name: str, price: float, kind: str = "level",
                 close_price: "float | None" = None) -> None:
            if name in _liq_map:
                if abs(_liq_map[name].get("price", 0) - price) > 1e-9:
                    _old = _liq_map[name].get("price")
                    _liq_map[name]["price"] = price
                    if close_price is not None:
                        _liq_map[name]["close_price"] = close_price
                    _changed.append(name)
                    _liq_events.append({
                        "kind": "liquidity-updated",
                        "time": now.isoformat(),
                        "name": name,
                        "old_price": _old,
                        "price": price,
                    })
                elif close_price is not None:
                    # Wick unchanged but the body extreme may have moved (e.g. a new lowest
                    # close without a new lowest low) → keep close_price current. No event
                    # (events key on the wick `price`), but flag _changed so daily.json is
                    # persisted with the updated body extreme.
                    _prev_cp = _liq_map[name].get("close_price")
                    if _prev_cp is None or abs(_prev_cp - close_price) > 1e-9:
                        _liq_map[name]["close_price"] = close_price
                        if name not in _changed:
                            _changed.append(name)
            else:
                _new_entry = {"name": name, "kind": kind, "price": price}
                if close_price is not None:
                    _new_entry["close_price"] = close_price
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
            # the (session-stable) day_start changes. Cache holds (high, low, close_hi, close_lo).
            if getattr(self, _daykey_attr) != _day_start_ts:
                setattr(self, _daykey_attr, _day_start_ts)
                _dh_hist = _tail[_tail.index >= _day_start_ts]
                setattr(self, _dayhl_attr, (
                    (float(_dh_hist["High"].values.max()), float(_dh_hist["Low"].values.min()),
                     float(_dh_hist["Close"].values.max()), float(_dh_hist["Close"].values.min()))
                    if not _dh_hist.empty else (None, None, None, None)
                ))
            _day_hl = getattr(self, _dayhl_attr)
            _dh = _mmax(_day_hl[0], _today_hi)
            _dl = _mmin(_day_hl[1], _today_lo)
            _dch = _mmax(_day_hl[2], _today_chi)
            _dcl = _mmin(_day_hl[3], _today_clo)
        else:
            _dh = _today_hi
            _dl = _today_lo
            _dch = _today_chi
            _dcl = _today_clo
        if _dh is not None and _dl is not None:
            _set("day_high", _dh, close_price=_dch)
            _set("day_low",  _dl, close_price=_dcl)
            _set("day_mid",  (_dh + _dl) / 2.0)

        # ── Week H/L ───────────────────────────────────────────────────────────
        # Week may span multiple sessions; the constant hist contribution
        # [week_start, session_start) is computed once and combined with today's extreme.
        _week_start = self._week_start_ts(now)
        if getattr(self, _weekkey_attr) != _week_start:
            setattr(self, _weekkey_attr, _week_start)
            _wh_hist = _tail[_tail.index >= _week_start]
            setattr(self, _weekhl_attr, (
                (float(_wh_hist["High"].values.max()), float(_wh_hist["Low"].values.min()),
                 float(_wh_hist["Close"].values.max()), float(_wh_hist["Close"].values.min()))
                if not _wh_hist.empty else (None, None, None, None)
            ))
        _week_hl = getattr(self, _weekhl_attr)
        _wh = _mmax(_week_hl[0], _today_hi)
        _wl = _mmin(_week_hl[1], _today_lo)
        _wch = _mmax(_week_hl[2], _today_chi)
        _wcl = _mmin(_week_hl[3], _today_clo)
        if _wh is not None and _wl is not None:
            _set("week_high", _wh, close_price=_wch)
            _set("week_low",  _wl, close_price=_wcl)
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
            if getattr(self, _sliver_attr) is None:
                _s18 = pd.Timestamp(_cme_session_start(now))
                setattr(self, _sliver_attr, _tail[_tail.index >= _s18])
            _sliver18 = getattr(self, _sliver_attr)
            _sess_src = (
                pd.concat([_sliver18, today_mnq])
                if not _sliver18.empty else today_mnq
            )
            _sbars = _session_bars(_sess_src, _active_sess, _sess_today)
            if not _sbars.empty:
                _sh = float(_sbars["High"].max())
                _sl = float(_sbars["Low"].min())
                _sch = float(_sbars["Close"].max())
                _scl = float(_sbars["Close"].min())
                _set(f"{_active_sess}_high", _sh, close_price=_sch)
                _set(f"{_active_sess}_low", _sl, close_price=_scl)

        # ── FVG visited prune ──────────────────────────────────────────────────
        _to_remove = []
        for _l in _liq:
            if _l.get("kind") != "fvg":
                continue
            # keep:True FVGs (yesterday-session fill universe) survive the visited prune —
            # they must remain fill targets through the whole session; the fill state
            # machine handles single-fire + re-arm.
            if _l.get("keep"):
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
        for _f in self._extend_instrument_fvg_frames(now, today_mnq, fvg_specs):
            if _f["name"] not in _liq_map:
                _liq.append(_f)
                _liq_map[_f["name"]] = _f
                _changed.append(_f["name"])

        if _changed or _to_remove:
            _state[liq_key] = list(_liq_map.values())
            _smt_state.save_daily(_state)
            # Keep _ext_levels current for the sweep check (MNQ levels only — the
            # cross-instrument sweep logic uses MNQ bars/levels).
            if liq_key == "liquidities":
                self._ext_levels = [
                    (l["name"], float(l["price"]))
                    for l in _state[liq_key]
                    if l.get("kind") == "level" and l.get("price") is not None
                ]

        return _liq_events

    def _extend_fvg_frames(self, now: pd.Timestamp, today_mnq: pd.DataFrame) -> list[dict]:
        """MNQ 1hr/4hr live-FVG pass (thin wrapper over the instrument-generic helper).

        Boundary tracking is timestamp-based rather than minute==0, so a missed tick
        or a data gap catches up on every boundary since the last processed one. Only
        the 3-bar windows ending at newly appended bars are tested — an FVG can only
        ever complete at its 3rd bar, so older windows need no re-scan (and a pruned
        FVG can never resurrect). The visited check inside _detect_fvgs runs against
        today's 1m bars after formation, matching the daily-scan semantics.
        """
        return self._extend_instrument_fvg_frames(
            now, today_mnq,
            (("1h", "_fvg_1hr", "_fvg_done_1hr"),
             ("4h", "_fvg_4hr", "_fvg_done_4hr")),
        )

    def _extend_instrument_fvg_frames(
        self, now: pd.Timestamp, today_mnq: pd.DataFrame, specs: tuple,
    ) -> list[dict]:
        """Append just-completed TF bars (built from live 1m) to the rolling FVG frames
        named in `specs` = ((freq, frame_attr, done_attr), ...) and return the FVGs
        completed by those new bars. Instrument-agnostic (MNQ or MES)."""
        from daily import _detect_fvgs

        out: list[dict] = []
        for _freq, _frame_attr, _done_attr in specs:
            _cur = self._floor(now, _freq)  # bars labeled >= _cur are still forming
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

    # ── SMT V2 detection orchestration ──────────────────────────────────────
    @staticmethod
    def _minute_bar_from_frame(df: pd.DataFrame, minute: pd.Timestamp) -> "pd.Series | None":
        """Return the COMPLETED 1m OHLC(V) row labelled exactly `minute` from `df`, or None.

        Used by the per-second (1s/live) rollover path to recover a just-finished minute's
        finalized bar from today_mnq/today_mes (where it sits as a normal indexed row once
        the next minute has begun). Aggregates defensively in case the frame carries sub-
        minute rows for that label (it normally carries one 1m row)."""
        if df is None or len(df) == 0:
            return None
        _step = pd.Timedelta("1min")
        _lo = df.index.searchsorted(minute, side="left")
        _hi = df.index.searchsorted(minute + _step, side="left")
        if _hi <= _lo:
            return None
        _win = df.iloc[_lo:_hi]
        _out = {
            "Open":  float(_win["Open"].iloc[0]),
            "High":  float(_win["High"].values.max()),
            "Low":   float(_win["Low"].values.min()),
            "Close": float(_win["Close"].iloc[-1]),
        }
        if "Volume" in _win.columns:
            _out["Volume"] = float(_win["Volume"].values.sum())
        return pd.Series(_out)

    def _run_completed_bar_detection(
        self,
        now: pd.Timestamp,
        mnq_bar_row,
        mes_bar_row,
        today_mnq: pd.DataFrame,
        today_mes: pd.DataFrame,
        *,
        pre_daily: "dict | None",
        bar_complete: "bool | None",
    ) -> list[dict]:
        """R3 driver: run SMT detection on COMPLETED 1m bars only, deterministically across
        replay modes, while order execution stays at 1s cadence (handled by the caller).

        Two paths, both ending in a single _run_smt_v2_detection call per completed minute
        passed through the _should_emit_smt gate:

        * bar_complete is True/None (1m regression, completed-bar drivers, legacy per-minute
          unit tests): `mnq_bar_row` IS the finalized bar for `now`'s minute. Detect it
          immediately, mirroring the pre-R3 order exactly (pre_daily reflects through the
          prior bar; today_mnq carries the completed bar).

        * bar_complete is False (per-second 1s / live): `mnq_bar_row` is an intra-minute
          PARTIAL. Detection must NOT run on it. Instead, when the minute rolls over, the
          PRIOR minute is now complete: reconstruct its finalized bar from today_mnq/
          today_mes and detect it against the daily snapshot captured at the START of that
          prior minute (state through the bar before it). This makes the 1s/live smt-div
          stream identical to the 1m stream (modulo exact sub-bar timestamp).

        Returns the emitted smt-div events (already gated; the caller emits + appends)."""
        _now_minute = now.floor("1min")

        # 1m-regression / completed-bar / legacy-per-minute path: detect this bar now.
        if bar_complete or bar_complete is None:
            # Record this minute's pre-update snapshot for symmetry (unused on this path,
            # but keeps the per-minute snapshot coherent if a caller mixes modes).
            self._detect_pre_daily = pre_daily
            self._detect_pre_daily_minute = _now_minute
            if not self._should_emit_smt(_now_minute):
                self._last_detect_minute = _now_minute
                return []
            _this_5m = _now_minute.floor("5min")
            _is_5m = (_now_minute.minute % 5 == 0) and (_this_5m != self._last_detect_5m)
            if _is_5m:
                self._last_detect_5m = _this_5m
            _out = self._run_smt_v2_detection(
                now, mnq_bar_row, mes_bar_row, today_mnq, today_mes, _is_5m,
                pre_daily=pre_daily,
            )
            self._last_detect_minute = _now_minute
            self._last_emitted_5m_bucket = _this_5m
            return _out

        # Per-second (1s/live) path: detect the just-completed PRIOR minute on rollover.
        events: list[dict] = []
        _prev_minute = self._detect_pre_daily_minute
        if (
            _prev_minute is not None
            and _now_minute > _prev_minute
            and _prev_minute != self._last_detect_minute
        ):
            # The bar at _prev_minute is now finalized inside today_mnq/today_mes.
            _mnq_done = self._minute_bar_from_frame(today_mnq, _prev_minute)
            _mes_done = self._minute_bar_from_frame(today_mes, _prev_minute)
            if _mnq_done is not None:
                if self._should_emit_smt(_prev_minute):
                    _prev_5m = _prev_minute.floor("5min")
                    _is_5m_prev = (
                        (_prev_minute.minute % 5 == 0)
                        and (_prev_5m != self._last_detect_5m)
                    )
                    if _is_5m_prev:
                        self._last_detect_5m = _prev_5m
                    # MES may have no bar this minute (feed gap) — pass a degenerate empty
                    # Series; _run_smt_v2_detection skips cleanly via _bar_row_has_ohlc.
                    _mes_arg = _mes_done if _mes_done is not None else pd.Series(dtype=float)
                    events = self._run_smt_v2_detection(
                        _prev_minute, _mnq_done, _mes_arg,
                        today_mnq, today_mes, _is_5m_prev,
                        pre_daily=self._detect_pre_daily,
                    )
                    self._last_emitted_5m_bucket = _prev_5m
                self._last_detect_minute = _prev_minute

        # Capture THIS minute's pre-update snapshot (state through the prior completed bar)
        # the first time we see the minute, so the next rollover detects this minute's bar
        # against the correct prior-bar liquidity state.
        if self._detect_pre_daily_minute != _now_minute:
            self._detect_pre_daily = pre_daily
            self._detect_pre_daily_minute = _now_minute

        return events

    def finalize_detection(
        self, today_mnq: pd.DataFrame, today_mes: pd.DataFrame,
    ) -> list[dict]:
        """R3: detect the FINAL pending minute in the per-second (1s/live) path.

        The rollover path detects minute M only once minute M+1 begins; the very last minute
        of a session never sees a successor call, so its completed bar would be missed. Call
        this once after the per-second loop ends (1s backtest) to detect that trailing
        minute against its start-of-minute daily snapshot — making the 1s tail match the 1m
        stream, which detects every completed bar including the last. Idempotent: a minute
        already detected (or already flushed) is not re-detected. Emits via self._emit and
        also returns the events for the caller's bookkeeping. No-op for the bar_complete=True
        (1m) path, which already detects every bar inline."""
        events: list[dict] = []
        _pending = self._detect_pre_daily_minute
        if _pending is None or _pending == self._last_detect_minute:
            return events
        _mnq_done = self._minute_bar_from_frame(today_mnq, _pending)
        if _mnq_done is None:
            return events
        _mes_done = self._minute_bar_from_frame(today_mes, _pending)
        if self._should_emit_smt(_pending):
            _p5 = _pending.floor("5min")
            _is_5m_p = (_pending.minute % 5 == 0) and (_p5 != self._last_detect_5m)
            if _is_5m_p:
                self._last_detect_5m = _p5
            _mes_arg = _mes_done if _mes_done is not None else pd.Series(dtype=float)
            events = self._run_smt_v2_detection(
                _pending, _mnq_done, _mes_arg, today_mnq, today_mes, _is_5m_p,
                pre_daily=self._detect_pre_daily,
            )
            self._last_emitted_5m_bucket = _p5
            for _sd in events:
                self._emit(_sd)
        self._last_detect_minute = _pending
        return events

    def _should_emit_smt(self, now: pd.Timestamp) -> bool:
        """Emit-gate predicate for completed-1m SMT detection (R3).

        Called once per COMPLETED 1m bar (the per-second / 1s / live path runs detection
        only on a minute rollover; 1m regression runs it once per bar). Currently returns
        ``True`` for every completed 1m bar, so detection fires on each finished minute in
        all replay modes — keeping the smt-div stream identical between 1m and 1s/live.

        HOOK (do NOT enable now): a future tightening makes detection emit only when the
        5m bucket has changed since the last emission. The seam is already wired — flip
        ``_ENABLE_5M_GATE`` to True and the body below restricts emission to one bar per
        5m bucket. `_last_emitted_5m_bucket` is maintained by the caller after a True
        verdict, so a unit test can assert this predicate is consulted per completed bar.
        """
        _ENABLE_5M_GATE = False  # R3 out-of-scope: 5m-only emission is a future toggle.
        if not _ENABLE_5M_GATE:
            return True
        _bucket = now.floor("5min")
        return _bucket != self._last_emitted_5m_bucket

    @staticmethod
    def _completed_tf_bar(today_df: pd.DataFrame, now: pd.Timestamp, tf: str) -> "dict | None":
        """Resample today's 1m bars to `tf` and return the just-COMPLETED bar (the one
        labelled at now.floor(tf) - tf) as an OHLC dict, or None if it does not exist.

        Returns the bar whose window ended exactly at the current TF boundary — only
        meaningful when `now` sits on that boundary (the caller guards this)."""
        if today_df is None or today_df.empty:
            return None
        _step = pd.Timedelta(tf)
        _label = now.floor(tf) - _step
        _lo = today_df.index.searchsorted(_label, side="left")
        _hi = today_df.index.searchsorted(_label + _step, side="left")
        _win = today_df.iloc[_lo:_hi]
        if _win.empty:
            return None
        return {
            "time": _label.isoformat(),
            "open": float(_win["Open"].iloc[0]),
            "high": float(_win["High"].values.max()),
            "low": float(_win["Low"].values.min()),
            "close": float(_win["Close"].iloc[-1]),
        }

    def _run_smt_v2_detection(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,
        mes_bar_row: pd.Series,
        today_mnq: pd.DataFrame,
        today_mes: pd.DataFrame,
        is_5m: bool,
        pre_daily: "dict | None" = None,
        warmup_now: "pd.Timestamp | None" = None,
    ) -> list[dict]:
        """SMT V2: run per-1m detection (regular + fill every bar, hidden at 15m/30m),
        accumulate into the buffers, run the flat-gated cadence-appropriate reference
        consumer, drain the accumulator after consumers, and persist to smts.json.

        Returns one `smt-div` signal event per newly-found SMT/fill this bar (for the
        caller to emit + log), so the new mechanism's SMTs are visible in events.jsonl /
        the regression plot. Strategy behavior is otherwise unchanged (no consumer acts on
        these yet). Mutates self._smt_buffer / self._detect_state / self._pending_watch and
        writes smts.json. Total: never raises on degenerate input.

        `warmup_now` (pre-startup replay): when set to the startup timestamp, the emitted
        smt-div events are tagged `source="v2-warmup"`, `warmup=True`, and `logged_at=<now>`
        while their `time` stays the REAL pre-startup occurrence bar — so a late start that
        replays `[session_open, now)` records SMTs it would otherwise have missed, and
        post-session analysis can tell they were found at startup rather than live. The
        detection logic and persisted detect_state are identical to the live per-bar path.
        """
        from smt_detect import (
            eligible_levels, detect_regular_smts, detect_hidden_smts, detect_fill_smts,
        )

        # Refinement #2: use the PRE-update daily snapshot captured by the caller (before
        # _update_dynamic_liquidities ran), so levels AND FVG pairing reflect the prior-bar
        # state — a one-bar lag that makes a "touch" mean a genuine take-out of the prior
        # extreme rather than equalling the just-updated running extreme. Fall back to a
        # fresh load only if no snapshot was passed (defensive; callers always pass it).
        # SMT compares MNQ vs MES; if either instrument has no bar this minute (a
        # degenerate empty Series from a feed gap), there is nothing to diverge against —
        # skip cleanly instead of KeyError on mes_bar_row["High"]/["Close"] below.
        if not (_bar_row_has_ohlc(mnq_bar_row, "High", "Low", "Close")
                and _bar_row_has_ohlc(mes_bar_row, "High", "Low", "Close")):
            return []

        daily = pre_daily if pre_daily is not None else _smt_state.load_daily_ro()
        # Universe (B) fixed levels are an additive block merged ONLY here (never into the
        # strategy's `liquidities`/_ext_levels), so SMT detection sees the prev-day/week
        # extremes while trades stay unchanged. eligible_levels dedups by name.
        liq_mnq = (daily.get("liquidities", []) or []) + (daily.get("liquidities_universe", []) or [])
        liq_mes = (daily.get("liquidities_mes", []) or []) + (daily.get("liquidities_universe_mes", []) or [])

        levels_mnq = eligible_levels(liq_mnq, now)
        levels_mes = eligible_levels(liq_mes, now)

        # MNQ level-name → price, for the smt-div signal's `mnq_div_price` (drives the
        # plot label/scope for wick/body SMTs; None for fills, which reference an FVG zone).
        _mnq_lvl_px = {
            l["name"]: float(l["price"])
            for l in liq_mnq
            if l.get("kind") == "level" and l.get("price") is not None
        }

        mnq_bar = {
            "time": now.isoformat(),
            "high": float(mnq_bar_row["High"]), "low": float(mnq_bar_row["Low"]),
            "close": float(mnq_bar_row["Close"]),
        }
        mes_bar = {
            "time": now.isoformat(),
            "high": float(mes_bar_row["High"]), "low": float(mes_bar_row["Low"]),
            "close": float(mes_bar_row["Close"]),
        }

        records: list[dict] = []
        _new, self._detect_state = detect_regular_smts(
            levels_mnq, levels_mes, mnq_bar, mes_bar, self._detect_state)
        records += _new

        paired = self._pair_fvgs(liq_mnq, liq_mes)
        _new, self._detect_state = detect_fill_smts(
            paired, mnq_bar, mes_bar, self._detect_state)
        records += _new

        # Hidden (body) SMTs: evaluate the just-completed 1m bar's CLOSE each minute. (15m/30m
        # fired too late — even 5m can fire early; the 1m close hints a trend change earliest.)
        for _tf, _tag in (("1min", "1m"),):
            _floor = self._floor(now, _tf)
            if now != _floor:
                continue  # not on this TF boundary
            if self._hidden_done.get(_tf) == _floor:
                continue  # already processed this boundary
            self._hidden_done[_tf] = _floor
            _mnq_tf = self._completed_tf_bar(today_mnq, now, _tf)
            _mes_tf = self._completed_tf_bar(today_mes, now, _tf)
            if _mnq_tf is None or _mes_tf is None:
                continue
            _new, self._detect_state = detect_hidden_smts(
                levels_mnq, levels_mes, _mnq_tf, _mes_tf, _tag, self._detect_state)
            records += _new

        # Refinement #1: dedup near-coincident level SMTs (same side, level prices within
        # DEDUP_TOL_PTS) down to the single highest-scope level. Applied to `records` BEFORE
        # both buffering and emission so the buffers and emitted smt-div events stay in sync.
        # Fills are exempt (handled inside _dedup_level_smts).
        records = _dedup_level_smts(records, _mnq_lvl_px)

        # --- SMT V2 Phase 2 SHADOW active-set compute (zero behavior change) ----------
        # Compute the relevance-filtered active set + dominant from this bar's fresh
        # records and store them under hypothesis.json debug keys ONLY. This does NOT
        # touch `direction` or any field the strategy/executor reads. The whole block is
        # exception-isolated so a defect here can NEVER break the live detection/direction
        # path (silent — no prints, no re-raise). Phase 3 will remove the blanket swallow.
        try:
            import smt_detect as _smt_detect
            _pos = _smt_state.load_position()
            _active_pos = _pos.get("active") or {}
            _flat_shadow = not _active_pos
            _backing_tier = _active_pos.get("backing_tier") if not _flat_shadow else None
            _hyp = _smt_state.load_hypothesis()
            _active = _hyp.get("smt_active_set", []) or []
            _ctargets = {
                "cautious_price_initial":   _hyp.get("cautious_price_initial", ""),
                "cautious_price_secondary": _hyp.get("cautious_price_secondary", ""),
            }
            # Invalidate BEFORE ingest: drop active records that are fulfilled/gone/
            # INVALIDATED (Part B) or already flagged fulfilled. A collapsed record carries
            # `keys` (wick+body folded) — aggregate over ALL its underlying detect keys via
            # `collapsed_relevance` (ANY fulfilled → fulfilled; ANY invalidated → invalidated;
            # ALL gone → gone). Only `unfulfilled` records survive (fulfilled/invalidated/
            # gone are all terminal). `invalidated` is forward-compatible: absent producer
            # flag → smt_status returns unfulfilled → no drop.
            _all_keys = [k for r in _active for k in (r.get("keys") or [r.get("key")])]
            _status = _smt_detect.smt_status(_all_keys, self._detect_state)
            _active = [
                r for r in _active
                if _hyp_mod.collapsed_relevance(r, _status) == "unfulfilled"
                and not r.get("fulfilled")
                and not r.get("invalidated")
            ]
            # Ingest fresh records WITHOUT the internal Rule A pass (apply_rule_a_step=False)
            # so we can run Rule A explicitly next and capture its superseded-event trail.
            _new_recs = [_hyp_mod.to_record(r) for r in records]
            _collapsed = _hyp_mod.ingest_smts(
                _new_recs, _active,
                flat=_flat_shadow, cautious_targets=_ctargets,
                backing_tier=_backing_tier, x_pts=_hyp_mod.RELEVANCE_X_PTS,
                apply_rule_a_step=False,
            )
            _events: list = []
            # Rule A — same-level latest-take-out-wins (capture the trail).
            _active, _a_events = _hyp_mod.apply_rule_a(_collapsed)
            _events.extend(_a_events)
            # MNQ close this bar — the adverse-move reference for Rule B / leg tracking.
            _now_close = float(mnq_bar_row["Close"])
            # Rule B (gated; default OFF) — recency-trend cross-tier suppression.
            _active, _b_events = _hyp_mod.apply_rule_b(
                _active, now_close=_now_close, enabled=_hyp_mod.RULE_B_ENABLED,
                min_age_min=_hyp_mod.RULE_B_MIN_AGE_MIN,
                adverse_pts=_hyp_mod.RULE_B_ADVERSE_PTS,
                tier_slack=_hyp_mod.RULE_B_TIER_SLACK,
            )
            _events.extend(_b_events)
            # Leg-scoped suppression — track the most-recently swept-and-reclaimed FIXED
            # level (dynamic; from this bar's liquidities) and suppress older counter-trend
            # SMTs until price returns to the swept origin. Leg state persists across bars.
            _leg_state = _hyp.get("smt_leg_state") or {}
            _leg_state = _hyp_mod.update_leg(
                _leg_state, fixed_levels=liq_mnq,
                now_close=_now_close, now_time=now,
            )
            _active, _leg_events, _leg_state = _hyp_mod.suppress_counter_trend(
                _active, _leg_state, _now_close)
            _events.extend(_leg_events)
            _dom = _hyp_mod.dominant(_active)
            # --- GIL-32: standing SMT conviction set (separate from smt_active_set) ----
            # Maintain a SEPARATE standing-conviction set with its own lifecycle (residual
            # after fulfillment + birth grace + adverse-run sustain). Built from THIS bar's
            # fired divs (`records`) + a status map over the standing records' detect keys
            # (so fulfilled/gone are observed), NOT from the relevance-filtered `_active`.
            # Legacy `smt_active_set` / `_compute_smt_score_v2` / GIL-19 relevance untouched.
            import smt_conviction as _smt_conv
            _prev_conv = _hyp.get("smt_conviction_set", []) or []
            _conv_keys: list = []
            for _r in _prev_conv:
                if not isinstance(_r, dict):
                    continue
                _ks = _r.get("keys")
                if not _ks:
                    _rt = _r.get("type")
                    if _rt in ("wick", "body"):
                        _ks = [f"{_r.get('ref_name')}|{_r.get('direction')}|{_rt}"]
                    else:
                        _ks = [str(_r.get("ref_name"))]
                for _k in _ks:
                    if _k and _k not in _conv_keys:
                        _conv_keys.append(_k)
            _conv_status = _smt_detect.smt_status(_conv_keys, self._detect_state)
            _conv_set = _smt_conv.update_standing(
                _prev_conv, records, _conv_status,
                mnq_close=float(mnq_bar_row["Close"]), now_iso=now.isoformat(),
            )
            # --- GIL-32 Phase-1b: same-liquidity reversal locks ------------------------
            # Maintained in lockstep with the conviction set but with a SEPARATE durable
            # lifecycle keyed on LEVEL ACCEPTANCE (not the conviction set's adverse-run, which
            # evicts on the very stop-run pop the SMT predicts will reverse). advance() ages /
            # releases existing locks (acceptance break, fulfill, or age-out); arm() opens a
            # lock when the current hypothesis is a reversal on a swept level that still has a
            # live same-side standing SMT. Status for the lock keys is queried directly against
            # detect_state so a fulfill is seen even after the conviction set dropped the SMT.
            import smt_reversal_lock as _smt_lock
            _prev_locks = _hyp.get("smt_reversal_locks", []) or []
            _lock_keys: list = [k for _l in _prev_locks for k in (_l.get("keys") or [])]
            _lock_status = _smt_detect.smt_status(_lock_keys, self._detect_state)
            # (1) advance existing locks (level-acceptance / fulfill / age release), then
            # (2) ingest THIS bar's reversal-SMT fires (open new non-protecting locks with their
            #     own durable level-acceptance lifecycle — so a bearish high-SMT survives the
            #     manipulation pop that the fire-close adverse-run would have evicted it on).
            _locks = _smt_lock.advance(
                _prev_locks, _mnq_lvl_px, _lock_status,
                mnq_close=float(mnq_bar_row["Close"]), now_iso=now.isoformat(),
            )
            _locks = _smt_lock.ingest_fires(_locks, records, _mnq_lvl_px, now.isoformat())
            # (3) promote to PROTECTING when the CURRENT hypothesis is a reversal on the locked
            #     level (direction + last_liquidity == rule2b's swept level). Only protecting
            #     locks veto the opposite hypothesis on that liquidity.
            _hyp2 = _smt_state.load_hypothesis()
            _locks = _smt_lock.mark_protecting(
                _locks, _hyp2.get("direction"), _hyp2.get("last_liquidity"),
            )
            _hyp2["smt_active_set"] = _active
            _hyp2["smt_dominant"] = _dom
            _hyp2["smt_leg_state"] = _leg_state
            _hyp2["smt_suppressions"] = _events
            _hyp2["smt_conviction_set"] = _conv_set
            _hyp2["smt_reversal_locks"] = _locks
            _smt_state.save_hypothesis(_hyp2)

            # GIL-25 Phase 1.2 (cross-session carry, WRITE side): mirror the finalized
            # still-valid (unfulfilled) active set into pending_smts.json each completed 1m
            # bar. INSIDE the same exception-isolated shadow try/except so a defect here can
            # NEVER break detection. The next session's cold-start _ingest_pending_smts reads
            # this back, re-validates against the overnight gap, and seeds survivors. Shadow:
            # affects only `divs` (not direction) in this branch.
            # GIL-25 Phase 1.1.5: source LEVEL reversals from the PRODUCER lifecycle
            # (detect_state) rather than the relevance-gated active set, so a still-unfulfilled
            # reversal carries forward even after price has backed off far from its level (the
            # prime carry case — the relevance/proximity gate would otherwise drop it from
            # _active and it would never be written). GIL-25 Phase 1.5: FILLS now source from the
            # producer lifecycle too (their detect_state key is the bare FVG name + fill_a_fired/
            # fill_b_fired flags, distinct from the level flags). The read-side
            # revalidate_and_filter_pending re-derives fulfillment against the overnight gap
            # (terminal state is re-VALIDATED, not trusted as a stale flag) and dedups by
            # (ref_name, direction); fills skip the level-relative depletion backstop.
            _carry_session = cme_session_date(now).isoformat()
            _level_entries = self._detect_state_pending_entries(_carry_session)
            # GIL-25 Phase 1.5: source FILL carry from the producer lifecycle (detect_state),
            # mirroring the LEVEL carry, so a backed-off unfulfilled fill still carries forward.
            _fill_entries = self._detect_state_fill_entries(_carry_session)
            _smt_state.save_pending_smts(
                {"entries": _level_entries + _fill_entries, "schema": 1})
        except Exception:
            pass
        # --- end SHADOW block ---------------------------------------------------------

        # Map each newly-found record to an smt-div signal event for emission/plotting.
        # `type` is kept raw (wick / body / fill_a / fill_b) so the hover is precise; the
        # plot label collapses it to W / H / F. `source:"v2"` marks the new mechanism.
        sd_events: list[dict] = []
        for _r in records:
            _ev = {
                "kind":          "smt-div",
                "time":          _r["time"],
                "side":          _r["side"],
                "type":          _r["type"],
                # Fills carry a phase (enter|cross|retrace); levels have none.
                "phase":         _r.get("phase"),
                "timeframe":     _r["timeframe"],
                "price":         _r["mnq_price"],
                # Body SMTs carry their own body-extreme comparison level in the record
                # (mnq_lvl_price); wick SMTs / fills fall back to the wick level-price map.
                "mnq_div_price": _r.get("mnq_lvl_price", _mnq_lvl_px.get(_r["ref_name"])),
                # Pre-startup warm-up replay tags this as "v2-warmup" + warmup/logged_at;
                # the live per-bar path keeps the plain "v2" source. `time` is the real
                # occurrence bar in both cases.
                "source":        "v2-warmup" if warmup_now is not None else "v2",
                "leader":        _r["leader"],
                "ref_name":      _r["ref_name"],
            }
            if warmup_now is not None:
                _ev["warmup"] = True
                _ev["logged_at"] = warmup_now.isoformat()
            sd_events.append(_ev)

        self._smt_buffer.add(records, now)

        # Cadence: 09:30–10:30 ET → per-1m, else 5m boundary.
        _now_et = now.tz_convert(_ET) if now.tzinfo else now
        _t = _now_et.time()
        cadence = "1m" if (datetime.time(9, 30) <= _t <= datetime.time(10, 30)) else "5m"

        # Reference consumer: flat-gated. 1m cadence every bar; 5m cadence on the boundary.
        _flat = not _smt_state.load_position_ro().get("active")
        if _flat and (cadence == "1m" or (cadence == "5m" and is_5m)):
            self._pending_watch.ingest(self._smt_buffer.get_new(cadence))
        self._pending_watch.update(
            now, float(mnq_bar_row["Close"]), float(mes_bar_row["Close"]))

        # Drain the 5m accumulator AFTER consumers, at the 5m boundary.
        if is_5m:
            self._smt_buffer.drain_if_boundary(now)

        # Persist edge/re-arm state + retained set (live restart continuity).
        _smt_state.save_smts({
            "detect_state": self._detect_state,
            "watch": self._pending_watch.to_dict(),
        })

        # Adverse-run invalidation trail (debug-only; never plotted, never in sd_events or
        # golden events). Mirror the producer's reserved __invalidations__ list to a JSON
        # artifact under the current state prefix so a post-run agent can verify which SMTs
        # were invalidated, when, and why. Full snapshot — the list only grows within a run.
        # Write ONLY when a new event was appended this bar: a per-1s-bar full rewrite of a
        # growing list is O(n^2) I/O (~23k rewrites/run), and invalidations are rare.
        _inv = self._detect_state.get("__invalidations__")
        if _inv and len(_inv) > self._inv_written_n:
            import json as _json
            (paths.state_dir() / "smt_invalidations.json").write_text(
                _json.dumps(_inv, indent=2), encoding="utf-8")
            self._inv_written_n = len(_inv)

        return sd_events

    def _detect_state_pending_entries(self, session_date: str) -> "list[dict]":
        """GIL-25 Phase 1.1.5: build pending_smts entries for every fired & non-terminal LEVEL
        record in the producer lifecycle (`detect_state`), decoupled from the relevance-gated
        active set so an unfulfilled reversal carries forward even after price has backed off
        from its level.

        A level record (detect key ``ref_name|direction|rec_type``) is carried iff it is
        ``fired`` and not (``fulfilled`` | ``invalidated`` | ``superseded`` |
        ``retired_depleted``) — i.e. exactly the records ``smt_status`` would report as
        "unfulfilled". The swept-level anchor (``price``) and fire close (``fire_price``) come
        straight from detect_state; the cold-start ``revalidate_and_filter_pending`` re-derives
        terminal state against the overnight gap and dedups wick/body siblings (same
        ref_name+direction → newest fire_time). Reserved ``__*__`` keys and fills (separate
        lifecycle) are skipped. Total: malformed keys/states skipped; never raises.
        """
        try:
            from smt_detect import _level_class
        except Exception:
            return []
        out: list[dict] = []
        ds = self._detect_state if isinstance(self._detect_state, dict) else {}
        for skey, st in ds.items():
            if not isinstance(skey, str) or skey.startswith("__") or "|" not in skey:
                continue
            if not isinstance(st, dict) or not st.get("fired"):
                continue
            if (st.get("fulfilled") or st.get("invalidated")
                    or st.get("superseded") or st.get("retired_depleted")):
                continue
            parts = skey.split("|")
            if len(parts) != 3:
                continue
            ref_name, direction, rec_type = parts
            try:
                tier = _level_class(ref_name)[1]
            except Exception:
                tier = "session"
            price = st.get("level_price")
            if price is None:
                price = st.get("fire_level_price")
            fire_price = st.get("fire_mnq_close")
            if fire_price is None:
                fire_price = st.get("fire_price")
            out.append({
                "price":        price,
                "fire_price":   fire_price,
                "direction":    direction,
                "tier":         tier,
                "type":         rec_type,
                "timeframe":    None,
                "fire_time":    st.get("fire_time"),
                "ref_name":     ref_name,
                "side":         "bearish" if direction == "short" else "bullish",
                "leader":       st.get("fire_leader"),
                "mes_price":    None,
                "session_date": session_date,
                "valid":        True,
            })
        return out

    def _detect_state_fill_entries(self, session_date: str) -> "list[dict]":
        """GIL-25 Phase 1.5: build pending_smts entries for every FIRED FILL (FVG-divergence)
        record in the producer lifecycle (`detect_state`), mirroring
        `_detect_state_pending_entries` for levels. This replaces the old relevance-gated
        active-set fill carry so a backed-off but still-unfulfilled fill carries forward.

        A fill key is the bare FVG name (no ``|`` parts) whose state carries the fill lifecycle
        (`fill_a_fired` / `fill_b_fired`). A fill is carried iff it FIRED
        (`fill_a_fired` OR `fill_b_fired`). The carry fields (`fire_price`, `fire_time`,
        `fire_phase`, `fire_zone`, `fire_leader`, `direction`) come straight from the fire-state
        capture in `detect_fill_smts`. `price=None` (no swept level — also makes the read-side
        depletion backstop a no-op) and `tier="day"` (locked design pt 4: fulfillment only at the
        day tier). Reserved ``__*__`` keys and LEVEL keys (3 ``|``-parts) are skipped. Total:
        malformed keys/states skipped; never raises.
        """
        out: list[dict] = []
        ds = self._detect_state if isinstance(self._detect_state, dict) else {}
        for skey, st in ds.items():
            try:
                if not isinstance(skey, str) or skey.startswith("__") or "|" in skey:
                    continue
                if not isinstance(st, dict):
                    continue
                # A fill state carries the fill lifecycle flags; level states do not.
                if "fill_a_fired" not in st and "fill_b_fired" not in st:
                    continue
                if not (st.get("fill_a_fired") or st.get("fill_b_fired")):
                    continue
                direction = st.get("direction")
                phase = st.get("fire_phase")
                out.append({
                    "kind":         "fill",
                    "price":        None,
                    "fire_price":   st.get("fire_price"),
                    "direction":    direction,
                    "tier":         "day",
                    "type":         phase,
                    "phase":        phase,
                    "zone":         st.get("fire_zone"),
                    "timeframe":    None,
                    "fire_time":    st.get("fire_time"),
                    "ref_name":     skey,
                    "side":         "bearish" if direction == "short" else "bullish",
                    "leader":       st.get("fire_leader"),
                    "mes_price":    None,
                    "session_date": session_date,
                    "valid":        True,
                })
            except Exception:
                continue
        return out

    def _active_record_to_pending(self, rec: dict, session_date: str) -> dict:
        """GIL-25 Phase 1.2: map one finalized active-set record to a pending_smts entry.

        `price` = the swept MNQ level price (mnq_lvl_price, fallback mnq_price). `fire_price`
        = the MNQ close at fire — the adverse/fulfill reference for re-validation. The active
        record from to_record does NOT carry the fire close, so it is read from detect_state
        by the record's detect key (set at smt_detect.py:452 _detect_level_smts), with a
        fallback to mnq_price. Total: missing fields → None; never raises.
        """
        price = rec.get("mnq_lvl_price")
        if price is None:
            price = rec.get("mnq_price")
        # fire_price from detect_state by the record's detect key (collapsed → first of keys).
        fire_price = None
        ks = rec.get("keys") or [rec.get("key")]
        for k in ks:
            st = self._detect_state.get(k) if isinstance(self._detect_state, dict) else None
            if isinstance(st, dict) and st.get("fire_mnq_close") is not None:
                fire_price = st.get("fire_mnq_close")
                break
        if fire_price is None:
            fire_price = rec.get("mnq_price")
        return {
            "price":        price,
            "fire_price":   fire_price,
            "direction":    rec.get("direction"),
            "tier":         rec.get("tier"),
            "type":         rec.get("type"),
            "timeframe":    rec.get("timeframe"),
            "fire_time":    rec.get("time"),
            "ref_name":     rec.get("ref_name"),
            "side":         rec.get("side"),
            "leader":       rec.get("leader"),
            "mes_price":    rec.get("mes_price"),
            "session_date": session_date,
            "valid":        True,
        }

    def _ingest_pending_smts(
        self, now: pd.Timestamp, today_mnq_at_open: pd.DataFrame,
    ) -> None:
        """GIL-25 Phase 1.2 (cross-session carry, READ side). Cold-start only — called from
        on_session_start AFTER _warmup_replay_smts (so we MERGE into its smt_active_set writes)
        and BEFORE the first run_hypothesis (so `divs` sees the survivors).

        Loads pending_smts, re-validates each carried SMT against the overnight/gap MNQ window
        (fire_time, session_open) via the pure smt_detect.revalidate_and_filter_pending (age-cap
        + drop fulfilled/invalidated + dedup vs today's fresh active set), then MERGES the
        survivors into hypothesis.json["smt_active_set"] (read-modify-write only that field) and
        emits a `smt-carry` audit event per ingested/dropped entry. Exception-isolated like the
        shadow write block — a defect can NEVER break startup. Shadow-only (no direction wiring).
        """
        try:
            import smt_detect as _smt_detect
            pend = _smt_state.load_pending_smts()
            entries = pend.get("entries", []) or []
            if not entries:
                return
            session_open = pd.Timestamp(_cme_session_start(now))
            today_date = cme_session_date(now)
            existing = _smt_state.load_hypothesis().get("smt_active_set", []) or []
            survivors, audit = _smt_detect.revalidate_and_filter_pending(
                entries, self._hist_mnq_1m, session_open, today_date, existing,
                dedup_tol_pts=DEDUP_TOL_PTS,
            )
            if survivors:
                _hyp = _smt_state.load_hypothesis()
                _hyp["smt_active_set"] = (_hyp.get("smt_active_set", []) or []) + survivors
                _smt_state.save_hypothesis(_hyp)
            for a in audit:
                self._emit({
                    "kind":      "smt-carry",
                    "time":      now.isoformat(),
                    "reason":    a.get("reason"),
                    "ref_name":  a.get("ref_name"),
                    "direction": a.get("direction"),
                    "fire_time": a.get("fire_time"),
                    "source":    "v2-carry",
                })
        except Exception:
            pass

    def _seed_pre_session_invalidation(self, now: pd.Timestamp) -> None:
        """R2 Phase 1.1 (GIL-25): pre-seed `detect_state["__level_inv__"]` for PAST liquidities
        (prev-day / prev-week) whose resting liquidity was already taken out BEFORE this session
        opened. The live + warm-up latch only starts at session open, so a prior-session take-out
        is otherwise invisible and the depleted level fires spurious mid-session SMTs and lingers
        as a cautious/hypothesis target. For each prev-day/week level (per ticker) we scan the
        bars in (level.window_end, session_open) — the level's own source window cannot exceed it
        by construction, so this only catches a genuine post-formation cross — and mark it
        invalidated when the window extreme runs FULFILL_PTS[tier] beyond the level. Total: never
        raises (degenerate input → no-op)."""
        from smt_detect import _level_class, _sub, pre_session_depleted
        try:
            session_open = pd.Timestamp(_cme_session_start(now))
        except Exception:
            return
        daily = _smt_state.load_daily()
        sources = (
            ("mnq", self._hist_mnq_1m,
             (daily.get("liquidities", []) or []) + (daily.get("liquidities_universe", []) or [])),
            ("mes", self._hist_mes_1m,
             (daily.get("liquidities_mes", []) or []) + (daily.get("liquidities_universe_mes", []) or [])),
        )
        liv = self._detect_state.setdefault("__level_inv__", {})
        for inst, hist, levels in sources:
            if hist is None or getattr(hist, "empty", True):
                continue
            for l in levels:
                name = l.get("name", "")
                we = l.get("window_end")
                price = l.get("price")
                sub = _sub(name)
                if not name.startswith("prev") or we is None or price is None or sub is None:
                    continue
                try:
                    we_ts = pd.Timestamp(we)
                except Exception:
                    continue
                win = hist[(hist.index > we_ts) & (hist.index < session_open)]
                if win.empty:
                    continue
                tier = _level_class(name)[1]
                if pre_session_depleted(sub, float(price), tier, inst,
                                        float(win["High"].max()), float(win["Low"].min())):
                    ent = liv.setdefault(name, {"mnq": False, "mes": False})
                    if not ent.get(inst):
                        ent[inst] = True
                        self._detect_state.setdefault("__level_retirements__", []).append({
                            "time": session_open.isoformat(), "ref_name": name, "tier": tier,
                            "kind": "fixed", inst: True, "reason": "pre_session_seed",
                        })

    def _warmup_replay_smts(
        self,
        now: pd.Timestamp,
        today_mnq_at_open: pd.DataFrame,
    ) -> list[dict]:
        """Pre-startup SMT warm-up: replay this session's bars in [session_open, now)
        through the SAME per-bar SMT detection the live loop uses, so a late start catches
        SMTs that fired before the orchestrator came up.

        Mirrors the SMT-relevant slice of on_1m_bar for each pre-startup 1m bar — snapshot
        daily.json, update the MNQ + MES dynamic liquidities, then run _run_smt_v2_detection
        (with warmup_now=now so the emitted smt-div events are tagged source="v2-warmup",
        warmup=True, logged_at=now while their `time` stays the real occurrence bar). Trend /
        hypothesis / strategy / order paths are intentionally NOT run — this only populates
        detect_state and logs the pre-existing SMTs. detect_state / smts.json end up exactly
        as if the live loop had processed these bars. Returns the emitted warm-up smt-div
        events (already emitted via self._emit; returned for the caller's bookkeeping/tests).

        Idempotency / restart safety: the caller only invokes this on a genuine cold start
        for this session (detect_state empty). On a warm restart, detect_state already holds
        the per-level edge/fired state for these bars, so this is skipped to avoid re-firing.
        """
        events: list[dict] = []
        if today_mnq_at_open is None or today_mnq_at_open.empty:
            return events

        _ss = pd.Timestamp(_cme_session_start(now))
        # MNQ pre-startup session bars [session_open, now). today_mnq_at_open is already
        # masked to <= now by the caller; intersect with the session window defensively.
        _mnq = today_mnq_at_open[
            (today_mnq_at_open.index >= _ss) & (today_mnq_at_open.index < now)
        ]
        if _mnq.empty:
            return events
        # Monotonic-increasing index is required for the per-bar searchsorted windows.
        assert _mnq.index.is_monotonic_increasing, "warm-up MNQ index must be sorted ascending"

        # MES pre-startup session bars come from hist (the dispatcher only passes MNQ at
        # open); reconstruct the same [session_open, now) window from _hist_mes_1m.
        _mes_src = self._hist_mes_1m if self._hist_mes_1m is not None else pd.DataFrame()
        _mes = _mes_src[(_mes_src.index >= _ss) & (_mes_src.index < now)]

        for _ts in _mnq.index:
            _mnq_row = _mnq.loc[_ts]
            _mes_row = _mes.loc[_ts] if _ts in _mes.index else pd.Series(dtype=float)
            _today_mnq = _mnq[_mnq.index <= _ts]
            _today_mes = _mes[_mes.index <= _ts]

            # Same pre-update daily snapshot + dynamic-liquidity refresh order as on_1m_bar.
            _pre_daily = _smt_state.load_daily()
            self._update_dynamic_liquidities(_ts, _mnq_row, _today_mnq)
            if _bar_row_has_ohlc(_mes_row, "High", "Low", "Close"):
                self._update_mes_liquidities(_ts, _mes_row, _today_mes)

            _this_5m = _ts.floor("5min")
            _is_5m = (_ts.minute % 5 == 0) and (_this_5m != self._last_5m_processed)
            if _is_5m:
                self._last_5m_processed = _this_5m
                # R3: keep the detection-side 5m tracker aligned with the execution-side one
                # for the warm-up's completed bars, so the live loop's first completed-bar
                # detection after startup continues the same 5m drain cadence.
                self._last_detect_5m = _this_5m

            for _sd in self._run_smt_v2_detection(
                _ts, _mnq_row, _mes_row, _today_mnq, _today_mes, _is_5m,
                pre_daily=_pre_daily, warmup_now=now,
            ):
                self._emit(_sd)
                events.append(_sd)
            # R3: record the warm-up's completed minute so the live rollover path does not
            # re-detect a minute the warm-up already processed.
            self._last_detect_minute = _ts
            self._detect_pre_daily = _pre_daily
            self._detect_pre_daily_minute = _ts

        # Persist detect_state immediately after the warm-up, before any live bars. The
        # per-bar path inside _run_smt_v2_detection already wrote smts.json each bar; this
        # is a defensive final flush so the cold-start guard (detect_state non-empty) holds
        # even if the last bar produced no new records.
        _smt_state.save_smts({
            "detect_state": self._detect_state,
            "watch":        self._pending_watch.to_dict(),
        })
        return events

    @staticmethod
    def _pair_fvgs(liq_mnq: list[dict], liq_mes: list[dict]) -> list[dict]:
        """Intersect MNQ↔MES 1hr FVGs by formation timestamp+side (the `fvg_<ts>_<side>`
        name). Returns the paired list consumed by detect_fill_smts."""
        def _parse(name: str) -> "tuple[str, str] | None":
            # fvg_{YYYYMMDD_HHMM}_{bull|bear}
            parts = name.split("_")
            if len(parts) < 4 or parts[0] != "fvg":
                return None
            return (f"{parts[1]}_{parts[2]}", parts[3])

        mnq_map: dict = {}
        for f in liq_mnq:
            if f.get("kind") != "fvg":
                continue
            k = _parse(f.get("name", ""))
            if k is not None:
                mnq_map[k] = f
        mes_map: dict = {}
        for f in liq_mes:
            if f.get("kind") != "fvg":
                continue
            k = _parse(f.get("name", ""))
            if k is not None:
                mes_map[k] = f

        paired: list[dict] = []
        for k in sorted(mnq_map.keys() & mes_map.keys()):
            _ts, _side = k
            paired.append({
                "name": f"fvg_{_ts}_{_side}",
                "side": _side,
                "mnq": {"top": mnq_map[k]["top"], "bottom": mnq_map[k]["bottom"]},
                "mes": {"top": mes_map[k]["top"], "bottom": mes_map[k]["bottom"]},
            })
        return paired

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

    def _seed_close_extremes(self, combined: pd.DataFrame, now: pd.Timestamp) -> dict:
        """CLOSE-based day/week extremes for the body-SMT seed.

        Mirrors compute_live_hl_mid's day/week WINDOWS (same lookback by ET hour) but uses
        Close max/min — no opening-spike outlier skip (the body extreme is a pure close
        extreme). Returns {day_high, day_low, week_high, week_low} as the highest/lowest
        Close over each window; keys absent when the window holds no bars. The per-bar
        dynamic pass overwrites these on the next bar, so this only seeds a sane start."""
        out: dict = {}
        if combined is None or combined.empty:
            return out
        _today = now.date()
        # Day window start (same hour rules as compute_live_hl_mid).
        _day_cal = _today if now.hour >= 18 else _today - datetime.timedelta(days=1)
        if now.hour >= 18:
            _day_hr = 6
        elif now.hour < 6:
            _day_hr = 12
        else:
            _day_hr = 18
        _day_start = pd.Timestamp(
            datetime.datetime(_day_cal.year, _day_cal.month, _day_cal.day, _day_hr, 0),
            tz="America/New_York",
        )
        _day_bars = combined[(combined.index >= _day_start) & (combined.index <= now)]
        if not _day_bars.empty:
            out["day_high"] = float(_day_bars["Close"].max())
            out["day_low"] = float(_day_bars["Close"].min())
        _week_start = self._week_start_ts(now)
        _week_bars = combined[(combined.index >= _week_start) & (combined.index <= now)]
        if not _week_bars.empty:
            out["week_high"] = float(_week_bars["Close"].max())
            out["week_low"] = float(_week_bars["Close"].min())
        return out

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
        current_5m = self._floor(now, "5min")
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
