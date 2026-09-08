"""The legacy `liquidities` list, reconstructed offline at an arbitrary boundary.

**Why this exists.** `hypothesis.compute_cautious_prices` — production's cautious ladder,
and plan 33 §2.1's initial target — is a pure function of `(direction, price, liquidities,
ath)`. Its INPUT is not available offline. That list is a `daily.json` artifact built in
three stages: `daily.run_daily_fixed` writes the static levels (TDO, TWO, prev1/prev2 day
H/L, unvisited 1h+4h FVGs); `session_pipeline.on_session_start` seeds week/day H/L/mid and
the completed sessions' extremes; then `_update_instrument_liquidities` recomputes on every
bar and prunes visited FVGs. The first two write legacy state and the third is a
`SessionPipeline` method over cached attributes reading `_smt_state.load_daily()`, so
neither is callable from here. Session state is archived only from **2026-08-25** onward,
so none of the study's 84 dates has one to read.

**This is not a second copy of the rule.** Every value comes from production's own
builders — `daily._detect_fvgs`, `daily._compute_two`, `daily._last_n_trading_dates`,
`daily._session_bars`, `strategy_smt.compute_tdo`, `hypothesis.compute_live_hl_mid` — and
what is reproduced here is only the ASSEMBLY ORDER those three stages use. That is what
makes a boundary-time rebuild equivalent to a per-bar accumulator: the per-bar pass is a
recomputation from data, not a path-dependent accumulation (`session_pipeline.py:1818` —
"today_df rows are all <= now, so its plain max/min IS the window extreme").

**Validated against the one archived snapshot.** At the 2026-08-25 11:59 ET boundary this
reproduces `<global>/sessions/2026-08-25/daily.json`'s `liquidities` EXACTLY: the same 19
names and every value equal, the 1h FVG (`fvg_20260818_0100_bear`, 30062.0/29919.25)
included — the component most at risk, since it is the only one whose membership depends on
the visited-pruning pass. Earlier boundaries differ only where they must: at 09:21 the
`ny_morning` extremes are still forming, and `fvg_20260825_0100_bull` is still unvisited
(it is pruned by 11:59). Both are correct time-dependence rather than error, and
`test_legacy_liquidities.py` pins all three readings.

It also independently reproduces plan 33 §3's own figure: at the 2026-09-02 fill
(29097.00, UP) `compute_cautious_prices` returns `cautious_price_initial = 29147.25`,
level `ny_morning_high`.

**Four deviations, none reconstructible, all reported rather than hidden.**

  * `invalidated_names=None`. `smts.json`'s `__level_inv__` (GIL-25's per-ticker depletion
    latch) is live state. It only ever REMOVES candidates, so the pool here is an upper
    bound, and `None` is documented as byte-identical to the legacy path.
  * `ath` = the historical data maximum, not `global.json`'s persisted value. It affects
    UP only, and only when the ATH sits inside the secondary cap.
  * `dist_shrinks = 0` — no failed entries assumed.
  * `keep:True` yesterday-session FVGs are omitted. Harmless:
    `compute_cautious_prices` skips them explicitly.

`now` is accepted and passed through, but it has NO effect on the ladder:
`OPEN_WINDOW_CAUTIOUS_SCALE_ENABLED` is False, so `_open_window_dist_scale` returns 1.0
unconditionally. It is threaded anyway so that flipping that flag does not silently make
this reconstruction wrong.

READ-ONLY. Nothing here writes `daily.json`, `smts.json`, or any other legacy state.
"""
from __future__ import annotations

import os

import pandas as pd

import paths

TZ = "America/New_York"
CONTRACT_SUBDIR = "2026-09"
_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}

#: `daily.run_daily_fixed`'s own FVG scan window.
FVG_LOOKBACK = pd.Timedelta(days=14)
#: The four 6-hour ET blocks `daily.TIME_WINDOWS` defines, in its order.
SESSIONS = ("asia", "london", "ny_morning", "ny_evening")
#: `compute_live_hl_mid`'s keys, in the order `on_session_start` appends them.
LIVE_KEYS = ("week_high", "week_low", "week_mid", "day_high", "day_low", "day_mid")


def load_1m(ticker: str = "MNQ", main_dir=None) -> pd.DataFrame:
    """The contract's 1m parquet, tz-aware ET.

    The 1m file and not the 1s-derived aggregate: this needs prev-day levels and a 14-day
    FVG scan, and the 1s parquets begin 2026-05-01, so the first three weeks of the study
    range would silently see a truncated history — the same trap `facts_source.py`
    documents for the candidate universe.
    """
    main_dir = main_dir or os.path.join(paths.general_main_dir(), CONTRACT_SUBDIR)
    df = pd.read_parquet(os.path.join(main_dir, f"{ticker}_1m.parquet"))
    df.index = pd.to_datetime(df.index)
    df.index = (df.index.tz_localize(TZ) if df.index.tz is None
                else df.index.tz_convert(TZ))
    return df.sort_index()


def reconstruct(bars_1m: pd.DataFrame, now: pd.Timestamp) -> list:
    """The MNQ `liquidities` list as of strictly BEFORE `now`.

    Strictly before, for the same reason `bundle_for_boundary` is exclusive: a level the
    fill's own bar establishes is not a level the decision could have read.
    """
    from daily import _compute_two, _detect_fvgs, _last_n_trading_dates, _session_bars
    from hypothesis import compute_live_hl_mid
    from strategy_smt import compute_tdo

    combined = bars_1m[bars_1m.index < now]
    if not len(combined):
        return []
    today = now.date()
    since = now - FVG_LOOKBACK

    # Completed bins only: an in-progress 1h/4h bar is not a bar the detector has seen.
    f1 = combined.resample("1h", label="left").agg(_AGG).dropna(subset=["Open"])
    f1 = f1[(f1.index >= since) & (f1.index < now.floor("1h"))]
    f4 = combined.resample("4h", label="left").agg(_AGG).dropna(subset=["Open"])
    f4 = f4[(f4.index >= since) & (f4.index < now.floor("4h"))]

    liq: list = []

    # -- stage 1: daily.run_daily_fixed's static levels ---------------------- #
    tdo = compute_tdo(combined, today)
    if tdo is not None:
        liq.append({"name": "TDO", "kind": "level", "price": float(tdo)})
    two = _compute_two(combined, today, now)
    if two is not None:
        liq.append({"name": "TWO", "kind": "level", "price": float(two)})

    for i, prior_date in enumerate(_last_n_trading_dates(today, 2), start=1):
        # The CME session window, not midnight-to-midnight: `run_daily_fixed` spells out
        # why (midnight-to-midnight would swallow the NEXT session's evening bars).
        pmid = pd.Timestamp(prior_date, tz=TZ)
        start = pmid - pd.Timedelta(hours=6)          # prior_date-1 18:00 ET
        end = pmid + pd.Timedelta(hours=17)           # prior_date   17:00 ET
        prior = combined.iloc[combined.index.searchsorted(start, "left"):
                              combined.index.searchsorted(end, "left")]
        if not prior.empty:
            liq.append({"name": f"prev{i}_day_high", "kind": "level",
                        "price": float(prior["High"].max())})
            liq.append({"name": f"prev{i}_day_low", "kind": "level",
                        "price": float(prior["Low"].min())})

    liq.extend(_detect_fvgs(f1, combined))
    liq.extend(_detect_fvgs(f4, combined))

    # -- stages 2+3: the session seed and the per-bar recomputation ---------- #
    live = compute_live_hl_mid(combined, now)
    for name in LIVE_KEYS:
        if name in live:
            liq.append({"name": name, "kind": "level", "price": float(live[name])})
    for sess in SESSIONS:
        bars = _session_bars(combined, sess, today)
        if bars.empty:
            continue                       # the window has not opened yet
        liq.append({"name": f"{sess}_high", "kind": "level",
                    "price": float(bars["High"].max())})
        liq.append({"name": f"{sess}_low", "kind": "level",
                    "price": float(bars["Low"].min())})
    return liq


def historical_ath(bars_1m: pd.DataFrame, now: pd.Timestamp) -> "float | None":
    """The data maximum before `now` — the stand-in for `global.json`'s persisted ATH.

    `run_daily_fixed` computes exactly this as `_hist_ath` and then takes `max()` with the
    persisted value, so this is the reconstructible half of production's own expression.
    """
    hist = bars_1m[bars_1m.index < now]
    return float(hist["High"].max()) if len(hist) else None
