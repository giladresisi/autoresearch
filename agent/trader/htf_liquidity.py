"""O5 — the 09:20 HTF liquidity context, as a pure function. SHADOW ONLY (record, no rule).

**What it is for (operator, 2026-10-16).** When the 09:20 thesis lands during or after a very
big earlier move (possibly multi-day), the nearby, not-yet-reached meaningful past liquidity
says whether the move is exhausted (2026-10-15: MNQ fell ~2000 pts from 10-06 into an
unreached Sep 17 daily bull FVG; the move was "not exhausted" until it was swept). This
module only makes that VISIBLE: `Analyzer` writes its result to `htf_context.json` at the
arm. It is not in the model prompt, not in the thesis cache key, and it changes no trading
behaviour. Research and rules come later.

**What it computes, per asset (MNQ primary, MES too), on 1m bars strictly before 09:20 ET:**

  ADR        mean (high - low) of the last `adr_sessions` completed 18:00->17:00 sessions.
             "Close" = |level - px_0919| <= `close_adr_mult` x ADR.
  big move   per direction, on 5m bars of the last `history_days` calendar days: the extreme
             E of the last `extreme_sessions` sessions (today's pre-09:20 part counts as one)
             and the farthest earlier 5m extreme (origin) such that no counter-retrace
             inside origin->E reached `part_k` x |E - origin| (the `o2_scan.big_move`
             search). big = size / px_0919 x 100 >= `big_pct` (premove_context's 1.14).
             state = "ongoing" if |px_0919 - E| < `retrace_frac` x size, else "retracing".
  levels     unreached liquidity formed before 09:20, within the close band, by distance:
               FVGs on 4h (18:00-aligned: 18/22/02/06/10/14 ET), daily (18:00->17:00) and
               weekly (Sun 18:00 -> Fri 17:00) bars. 3 consecutive bars of the series (a
               session break or weekend between them does NOT break the pattern — the
               operator's 10-15 4h FVG spans the 17:00 close). Bull: bar3.low > bar1.high,
               zone [bar1.high, bar3.low]; bear: bar3.high < bar1.low, zone [bar3.high,
               bar1.low]. Labelled by the MIDDLE bar's start. bar3 must be complete before
               09:20. Filled (dropped) once later price reaches the far edge; "partial" once
               it reached the near edge; else "untouched".
               Extremes of 6h sessions (18-00, 00-06, 06-12, 12-18 ET), days and weeks
               completed before 09:20 (today's 06-12 session is running and excluded),
               dropped once a later bar trades STRICTLY beyond (an equal print does not take
               it, the `htf_extremes` convention). Equal prices on one side collapse to one
               row, week over day over session6h, the rest kept as `aliases`.

**History.** Each tier needs its own lookback (4h FVG 30 d, daily FVG 120 d, weekly FVG 52 w,
ADR 20 sessions, sessions/days 10 trading days, weeks 8). A frame that does not reach back
far enough is NOT an error: the tier is computed on what there is and marked
`insufficient_history` with the days available (the trader's frames are capped at
`graft.EXECUTOR_HISTORY` = 17 d). ADR uses what there is down to `adr_min_sessions`.

**Never raises; pure.** No I/O and no clock: the only time it knows is the `now` it is handed
(bar time). Any failure is status UNKNOWN with a reason.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from agent.trader.premove_context import _normalise

TZ = "America/New_York"
OK, PARTIAL, UNKNOWN = "OK", "PARTIAL", "UNKNOWN"
TICKERS = ("MNQ", "MES")

TIER_FVG_4H, TIER_FVG_D, TIER_FVG_W = "fvg_4h", "fvg_daily", "fvg_weekly"
TIER_S6, TIER_DAY, TIER_WEEK = "session6h", "day", "week"
_EXT_RANK = {TIER_WEEK: 2, TIER_DAY: 1, TIER_S6: 0}
_S6_NAMES = {18: "asia", 0: "london", 6: "nyam", 12: "nypm"}   # 18-00, 00-06, 06-12, 12-18


@dataclass(frozen=True)
class HtfParams:
    boundary_hhmm: tuple = (9, 20)
    adr_sessions: int = 20
    adr_min_sessions: int = 5
    min_session_bars: int = 60       # a session with fewer 1m bars is a fragment, not a day
    close_adr_mult: float = 0.5
    history_days: float = 15.0       # big-move 5m window
    extreme_sessions: int = 5        # E = extreme of the last N sessions (today included)
    part_k: float = 0.5
    big_pct: float = 1.14
    retrace_frac: float = 0.25
    fvg_4h_days: float = 30.0
    fvg_daily_days: float = 120.0
    fvg_weekly_weeks: int = 52
    extreme_trading_days: int = 10   # 6h sessions and days
    extreme_weeks: int = 8
    history_slack_days: float = 3.0  # frame start may be this much later than the lookback
    tickers: tuple = TICKERS


DEFAULT_PARAMS = HtfParams()


@dataclass(frozen=True)
class HtfContext:
    status: str
    reason: str
    boundary: "pd.Timestamp | None" = None
    assets: dict = field(default_factory=dict)
    summary: "str | None" = None
    params: "dict | None" = None

    def to_dict(self) -> dict:
        return _jsonable({"status": self.status, "reason": self.reason,
                          "boundary": self.boundary, "summary": self.summary,
                          "assets": self.assets, "params": self.params})


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return None if math.isnan(x) else round(x, 4)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def compute(mnq_1m, mes_1m, now, params: "HtfParams | None" = None) -> HtfContext:
    """The HTF liquidity context at `now`'s 09:20 boundary. Never raises."""
    params = params or DEFAULT_PARAMS
    try:
        return _compute({"MNQ": mnq_1m, "MES": mes_1m}, now, params)
    except Exception as exc:                                  # total by contract
        return HtfContext(UNKNOWN, f"error: {type(exc).__name__}: {exc}",
                          params=asdict(params))


def _compute(frames: dict, now, params: HtfParams) -> HtfContext:
    if now is None:
        return HtfContext(UNKNOWN, "no bar time", params=asdict(params))
    now = pd.to_datetime(now)
    now = now.tz_localize(TZ) if now.tzinfo is None else now.tz_convert(TZ)
    bh, bm = params.boundary_hhmm
    boundary = now.normalize() + pd.Timedelta(hours=bh, minutes=bm)
    if now < boundary:
        return HtfContext(UNKNOWN, "called before the boundary", boundary=boundary,
                          params=asdict(params))
    assets = {}
    for tk in params.tickers:
        try:
            assets[tk] = _asset(frames.get(tk), boundary, params)
        except Exception as exc:
            assets[tk] = {"status": UNKNOWN, "reason": f"error: {type(exc).__name__}: {exc}"}
    oks = [tk for tk in assets if assets[tk].get("status") == OK]
    status = OK if len(oks) == len(assets) else PARTIAL if oks else UNKNOWN
    reason = "; ".join(f"{tk}: {a.get('reason')}" for tk, a in assets.items()
                       if a.get("status") != OK) or "ok"
    return HtfContext(status, reason, boundary=boundary, assets=assets,
                      summary=_summary(assets), params=asdict(params))


# -- bucketing (naive ET wall time: DST changes fall on a closed Sunday 02:00) ------------ #

def _local(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.set_axis(frame.index.tz_localize(None))


def _sess_date(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """18:00 ET belongs to the next calendar day's session."""
    return (idx + pd.Timedelta(hours=6)).normalize()


def _agg(df: pd.DataFrame, key) -> pd.DataFrame:
    """One row per bucket: high/low, the 1m stamps that printed them, first/last stamp, n."""
    src = df[["high", "low"]].assign(ts=df.index, key=np.asarray(key))
    g = src.groupby("key", sort=True)
    out = g.agg(high=("high", "max"), low=("low", "min"), first=("ts", "min"),
                last=("ts", "max"), n=("high", "size"))
    out["high_ts"] = src["ts"].values[_argext(src, "high", True)]
    out["low_ts"] = src["ts"].values[_argext(src, "low", False)]
    out.index.name = None
    return out


def _argext(src, col, is_max):
    """Positional index of the first max (min) of `col` per group, in group order."""
    vals = src[col].to_numpy(dtype=float)
    tmp = pd.DataFrame({"key": src["key"].to_numpy(), "v": vals if is_max else -vals,
                        "pos": np.arange(len(src))})
    tmp = tmp.sort_values(["key", "v", "pos"], ascending=[True, False, True], kind="stable")
    return tmp.drop_duplicates("key")["pos"].to_numpy()


def _tz(ts) -> pd.Timestamp:
    return pd.Timestamp(ts).tz_localize(TZ)


# -- per asset ----------------------------------------------------------------------- #

def _asset(frame, boundary: pd.Timestamp, params: HtfParams) -> dict:
    df = _normalise(frame)
    if df is None:
        return {"status": UNKNOWN, "reason": "no usable 1m frame"}
    df = df[df.index < boundary]
    last_min = boundary - pd.Timedelta(minutes=1)
    if not len(df) or df.index[-1] != last_min:
        return {"status": UNKNOWN, "reason": f"no {last_min.strftime('%H:%M')} bar"}
    px = float(df["close"].iloc[-1])
    loc = _local(df)
    b_loc = boundary.tz_localize(None)
    today_sd = _sess_date(pd.DatetimeIndex([b_loc - pd.Timedelta(minutes=1)]))[0]
    have_days = (boundary - df.index[0]).total_seconds() / 86400.0

    days = _agg(loc, _sess_date(loc.index))
    full_days = days[(days["n"] >= params.min_session_bars) & (days.index < today_sd)]

    adr_rows = full_days.tail(params.adr_sessions)
    adr = float((adr_rows["high"] - adr_rows["low"]).mean()) if len(adr_rows) else float("nan")
    if len(adr_rows) < params.adr_min_sessions:
        return {"status": UNKNOWN, "px_0919": px,
                "reason": f"{len(adr_rows)} completed sessions for ADR "
                          f"(< {params.adr_min_sessions})"}
    band = params.close_adr_mult * adr

    def coverage(need_days: float) -> dict:
        ok = have_days + params.history_slack_days >= need_days
        return {"status": "ok" if ok else "insufficient_history",
                "need_days": need_days, "have_days": round(have_days, 2)}

    tiers = {}
    levels = []

    # FVGs
    four = _agg(loc, (loc.index - pd.Timedelta(hours=2)).floor("4h") + pd.Timedelta(hours=2))
    four_end = four.index + pd.Timedelta(hours=4)
    four_complete = np.asarray(four_end <= b_loc)
    weeks = _agg(loc, _sess_date(loc.index) - pd.to_timedelta(
        _sess_date(loc.index).weekday, unit="D"))
    weeks = weeks[weeks["n"] >= params.min_session_bars]
    today_wk = today_sd - pd.Timedelta(days=today_sd.weekday())
    days_used = days[(days["n"] >= params.min_session_bars) | (days.index == today_sd)]

    for tier, bars, complete, lb_start, need, label_fmt in (
            (TIER_FVG_4H, four, four_complete,
             b_loc - pd.Timedelta(days=params.fvg_4h_days), params.fvg_4h_days,
             "%Y-%m-%d %H:%M"),
            (TIER_FVG_D, days_used, np.asarray(days_used.index < today_sd),
             b_loc - pd.Timedelta(days=params.fvg_daily_days), params.fvg_daily_days,
             "%Y-%m-%d"),
            (TIER_FVG_W, weeks, np.asarray(weeks.index < today_wk),
             b_loc - pd.Timedelta(weeks=params.fvg_weekly_weeks),
             params.fvg_weekly_weeks * 7.0, "W %Y-%m-%d")):
        found = _fvgs(bars, complete, lb_start, tier, label_fmt, px, band, adr)
        tiers[tier] = {**coverage(need), "n_unfilled": found[1], "n_close": len(found[0])}
        levels += found[0]

    # extremes
    hi_arr = loc["high"].to_numpy(dtype=float)
    lo_arr = loc["low"].to_numpy(dtype=float)
    suf_hi = np.maximum.accumulate(hi_arr[::-1])[::-1]
    suf_lo = np.minimum.accumulate(lo_arr[::-1])[::-1]
    idx = loc.index

    def after(end_excl):
        """(max high, min low) of bars at/after `end_excl` (naive local), or None."""
        j = idx.searchsorted(end_excl, side="left")
        if j >= len(idx):
            return None
        return suf_hi[j], suf_lo[j]

    completed_sds = list(full_days.index[-params.extreme_trading_days:])
    s6 = _agg(loc, loc.index.floor("6h"))
    s6 = s6[(s6.index + pd.Timedelta(hours=6) <= b_loc)
            & np.asarray(_sess_date(s6.index).isin(completed_sds + [today_sd]))]
    day_rows = full_days.tail(params.extreme_trading_days)
    wk_rows = weeks[weeks.index < today_wk].tail(params.extreme_weeks)

    ext_items = []
    for tier, rows, name_of, need in (
            (TIER_S6, s6,
             lambda k: f"{_S6_NAMES.get(k.hour, k.hour)} {_sess_date(pd.DatetimeIndex([k]))[0]:%Y-%m-%d}",
             params.extreme_trading_days * 1.5),
            (TIER_DAY, day_rows,
             lambda k: f"day {k:%Y-%m-%d}", params.extreme_trading_days * 1.5),
            (TIER_WEEK, wk_rows,
             lambda k: f"week {k:%Y-%m-%d}", params.extreme_weeks * 7.0)):
        n_unreached = 0
        for k, r in rows.iterrows():
            nxt = after(r["last"] + pd.Timedelta(minutes=1))
            for side, price, p_ts in (("above", float(r["high"]), r["high_ts"]),
                                      ("below", float(r["low"]), r["low_ts"])):
                if nxt is not None and ((side == "above" and nxt[0] > price) or
                                        (side == "below" and nxt[1] < price)):
                    continue
                n_unreached += 1
                ext_items.append({"tier": tier, "name": f"{name_of(k)} "
                                  f"{'high' if side == 'above' else 'low'}",
                                  "price": price, "formed_ts": _tz(p_ts),
                                  "period_start": _tz(r["first"]), "status": "untouched",
                                  "kind": "high" if side == "above" else "low"})
        tiers[tier] = {**coverage(need), "n_unreached": n_unreached}

    merged = {}
    for it in ext_items:
        key = (it["kind"], it["price"])
        cur = merged.get(key)
        if cur is None:
            merged[key] = {**it, "aliases": []}
        elif _EXT_RANK[it["tier"]] > _EXT_RANK[cur["tier"]]:
            merged[key] = {**it, "aliases": cur["aliases"] + [cur["name"]]}
        else:
            cur["aliases"].append(it["name"])
    n_close = {t: 0 for t in (TIER_S6, TIER_DAY, TIER_WEEK)}
    for it in merged.values():
        dist = it["price"] - px
        if abs(dist) > band:
            continue
        n_close[it["tier"]] += 1
        it.pop("kind")
        levels.append({**it, "side": "above" if dist > 0 else "below" if dist < 0 else "at",
                       "dist_pts": abs(dist), "dist_adr": abs(dist) / adr})
    for t, n in n_close.items():
        tiers[t]["n_close"] = n

    levels.sort(key=lambda d: d["dist_pts"])
    moves = {d: _big_move(df, boundary, d, px, params) for d in ("DOWN", "UP")}
    for m in moves.values():
        if m.get("size") is not None:
            m["size_adr"] = m["size"] / adr
    qual = [m for m in moves.values() if m.get("big")]
    big = max(qual, key=lambda m: m["size"])["direction"] if qual else None
    return {"status": OK, "reason": "ok", "px_0919": px, "adr": adr,
            "adr_sessions": len(adr_rows), "close_band_pts": band,
            "big_move": big, "moves": moves, "levels": levels, "tiers": tiers}


def _fvgs(bars, complete, lb_start, tier, label_fmt, px, band, adr):
    """([close unfilled FVG items], n unfilled in lookback). `bars` indexed by naive start,
    in time order; `complete` marks rows that are finished before the boundary. Later rows
    (including a running partial one) are the fill check."""
    if len(bars) < 3:
        return [], 0
    H = bars["high"].to_numpy(dtype=float)
    L = bars["low"].to_numpy(dtype=float)
    suf_hi = np.maximum.accumulate(H[::-1])[::-1]
    suf_lo = np.minimum.accumulate(L[::-1])[::-1]
    keys = bars.index
    out, n_unfilled = [], 0
    for i in range(1, len(bars) - 1):
        if not complete[i + 1] or keys[i] < lb_start:
            continue
        if L[i + 1] > H[i - 1]:
            kind, lo, hi = "bull", H[i - 1], L[i + 1]
        elif H[i + 1] < L[i - 1]:
            kind, lo, hi = "bear", H[i + 1], L[i - 1]
        else:
            continue
        later = i + 2 < len(bars)
        if kind == "bull":
            near, far = hi, lo
            filled = later and suf_lo[i + 2] <= far
            entered = later and suf_lo[i + 2] <= near
        else:
            near, far = lo, hi
            filled = later and suf_hi[i + 2] >= far
            entered = later and suf_hi[i + 2] >= near
        if filled:
            continue
        n_unfilled += 1
        if lo <= px <= hi:
            side, dist = "inside", 0.0
        else:
            dist = near - px
            side = "above" if dist > 0 else "below"
            dist = abs(dist)
        if dist > band:
            continue
        deepest = None
        if entered:
            deepest = float(suf_lo[i + 2] if kind == "bull" else suf_hi[i + 2])
        out.append({"tier": tier, "name": f"{tier} {kind} {keys[i].strftime(label_fmt)}",
                    "direction": kind, "price": float(near), "zone": [float(lo), float(hi)],
                    "side": side, "dist_pts": dist, "dist_adr": dist / adr,
                    "label_ts": _tz(keys[i]), "formed_ts": _tz(bars["last"].iloc[i + 1]),
                    "status": "partial" if entered else "untouched",
                    "deepest_fill": deepest})
    return out, n_unfilled


def _big_move(df: pd.DataFrame, boundary: pd.Timestamp, direction: str, px: float,
              params: HtfParams) -> dict:
    """The biggest move into the recent extreme in `direction` (see the module docstring)."""
    out = {"direction": direction, "big": False}
    d_norm = boundary.normalize()
    win_start = d_norm - pd.Timedelta(days=params.history_days)
    out["history"] = ("ok" if df.index[0] <= win_start + pd.Timedelta(days=params.history_slack_days)
                      else "insufficient_history")
    loc = _local(df[df.index >= win_start])
    sd = _sess_date(loc.index)
    recent = sorted(set(sd))[-params.extreme_sessions:]
    seg = loc[np.asarray(sd.isin(recent))]
    if not len(seg):
        out["reason"] = "no bars"
        return out
    if direction == "DOWN":
        e_ts, ext = seg["low"].idxmin(), float(seg["low"].min())
    else:
        e_ts, ext = seg["high"].idxmax(), float(seg["high"].max())
    f = loc[loc.index <= e_ts].resample("5min").agg({"high": "max", "low": "min"}).dropna()
    H, L = f["high"].to_numpy(dtype=float), f["low"].to_numpy(dtype=float)
    best = None
    if direction == "DOWN":
        sufmax = np.maximum.accumulate(H[::-1])[::-1]
        for i in np.where(H >= sufmax)[0]:
            mv = H[i] - ext
            if mv <= 0:
                continue
            rm = np.minimum.accumulate(L[i:])
            bounce = (H[i + 1:] - rm[:-1]).max() if len(H) - i > 1 else 0.0
            if bounce < params.part_k * mv and (best is None or mv > best[0]):
                best = (mv, f.index[i], float(H[i]), float(bounce))
    else:
        sufmin = np.minimum.accumulate(L[::-1])[::-1]
        for i in np.where(L <= sufmin)[0]:
            mv = ext - L[i]
            if mv <= 0:
                continue
            rm = np.maximum.accumulate(H[i:])
            dip = (rm[:-1] - L[i + 1:]).max() if len(L) - i > 1 else 0.0
            if dip < params.part_k * mv and (best is None or mv > best[0]):
                best = (mv, f.index[i], float(L[i]), float(dip))
    out.update({"extreme": ext, "extreme_ts": _tz(e_ts)})
    if best is None:
        out["reason"] = "no origin"
        return out
    mv, o_ts, o_px, max_counter = best
    after = loc[loc.index > e_ts]
    if direction == "DOWN":
        max_retrace = float(after["high"].max() - ext) if len(after) else 0.0
    else:
        max_retrace = float(ext - after["low"].min()) if len(after) else 0.0
    retrace = abs(px - ext)
    size_pct = mv / px * 100.0
    out.update({"origin": o_px, "origin_ts": _tz(o_ts), "size": float(mv),
                "size_pct": size_pct, "big": bool(size_pct >= params.big_pct),
                "max_counter_inside": max_counter,
                "retrace_pts": retrace, "retrace_frac": retrace / mv,
                "max_retrace_since_extreme": max(max_retrace, 0.0),
                "state": "ongoing" if retrace < params.retrace_frac * mv else "retracing"})
    return out


def _summary(assets: dict) -> str:
    parts = []
    for tk, a in assets.items():
        if a.get("status") != OK:
            parts.append(f"{tk}: {a.get('status')} ({a.get('reason')})")
            continue
        s = f"{tk}"
        big = a.get("big_move")
        if big:
            m = a["moves"][big]
            s += (f" big {big} {m['size']:.2f} pts ({m['size_pct']:.2f}%, "
                  f"{m['size_adr']:.1f}xADR) {m['state']}")
        else:
            s += " no big move"
        near = {}
        for lv in a.get("levels") or ():
            if lv["tier"] in (TIER_FVG_4H, TIER_FVG_D, TIER_FVG_W) and lv["side"] not in near:
                near[lv["side"]] = lv
        for side in ("above", "inside", "below"):
            if side in near:
                lv = near[side]
                s += f"; {side} {lv['name']} {lv['price']:.2f} ({lv['dist_adr']:.2f} ADR)"
        parts.append(s)
    return " | ".join(parts)
