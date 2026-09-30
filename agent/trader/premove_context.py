"""Plan 46 — the UNRELATED pre-move classifier, as a pure function (`l2-mechanisms.md` §11.5).

**A §11 CANDIDATE, not a rule.** Behind `UNRELATED_PATH_MODE`, default ``"off"``: with the
flag off nothing in the trader calls this module. ``"shadow"`` classifies and records
(`analyzer.Analyzer._premove`) without changing anything; ``"on"`` lets the Analyzer force the
thesis AGAINST an UNRELATED leg. That is the DIRECTION only: the take-profit stays the
ordinary T2 pick unless `MID_TARGET_ENABLED` is also set (operator decision 2026-09-30, see
the constant).

**What it reproduces.** `<global>/studies/premove_mid_retrace/context.py` + `build.py`,
exactly, on MNQ 1m bars strictly before 09:20 ET:

  leg        the current leg of a high/low zigzag on 1m bars from the 18:00 ET session open
             to 09:19, reversal threshold ``cut_pts_at_ref x px_0919 / ref_price``
             (`leg_zigzag`, a port of `build.leg_zigzag`). Origin A, extreme E.
  big        ``|E - A| / px_0919 x 100 >= big_pct`` (330 pts at 29000).
  PART       a zigzag on 5m bars over the previous ``history_days`` calendar days to 09:19,
             threshold ``part_k x size`` (`pivots_zigzag`, a port of
             `context.pivots_zigzag`): its current leg has the leg's direction, starts BEFORE
             today's 18:00 open, and is ``>= part_mult x size``.
  UNRELATED  big and not PART.

**No COUNTER branch.** The study's COUNTER context (the leg moves away from a 20-session
extreme; 07-30 and three older days) is NOT built: those days map to UNRELATED here. The
generic COUNTER test failed (a 10-session extreme reached the mid 83%), and only a
multi-month extreme behaved as the theory predicted — §11.5 open question 2.

**Never raises; never guesses.** Anything it cannot establish — no 09:19 bar, a frame that
does not reach back ``history_days`` (every replay before ~05-17: the 1s parquets start
2026-05-01), fewer than three big-scale pivots, malformed input — is ``UNKNOWN``, and the
caller falls through to today's path.

**Pure.** No I/O and no clock: the only time it knows is the ``now`` it is handed, which
is bar time. `test_gates_structural.RAW_SOURCE_BANS` greps this source for the usual
wall-clock and legacy-order names.

**Arm D (harness only).** `LEG_START_OVERRIDES` maps an ISO date to an operator leg start
``"HH:MM"`` ET (times from 18:00 on belong to the previous calendar day). When today's date
is present the origin is that minute's bar (its Low for an UP leg, its High for a DOWN leg)
and the extreme is re-taken from that bar to 09:19 in the auto leg's direction — the
labelling page's ``customLeg``. Size, big and PART are then computed on that leg. Default
``{}``: production is byte-identical.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

TZ = "America/New_York"

#: "off" | "shadow" | "on". Read at CALL time (module attribute), never cached, so a
#: harness can flip it between replays in one process. Anything else reads as "off".
UNRELATED_PATH_MODE = "off"
MODES = ("off", "shadow", "on")

#: The take-profit half: with the path "on", also take profit at the leg's mid at each fill
#: (`executor._premove_mid_pick`). **OFF by operator decision 2026-09-30** — the path
#: overrides the DIRECTION only for now; the operator manages the position by hand and is
#: reworking target selection / position management for these days. Measured (8 replayable
#: UNRELATED days, `.agents/ab_premove_unrelated.jsonl`): the mid target banked 06-29 and
#: 08-25, cut 05-18's +269 T2 winner to +90, and was irrelevant on four days that stopped
#: out first. Read when an Executor is built (one per plan), so a harness can flip it
#: between replays. Harness arms T and D set it True.
MID_TARGET_ENABLED = False

#: Harness arm D only: ISO date -> operator leg start "HH:MM" ET. Empty in production.
LEG_START_OVERRIDES: dict = {}

UNRELATED, PART, NOT_BIG, NO_LEG, UNKNOWN = "UNRELATED", "PART", "NOT_BIG", "NO_LEG", "UNKNOWN"
_OPPOSITE = {"UP": "DOWN", "DOWN": "UP"}


def path_mode() -> str:
    """The flag, validated: a typo must read as "off", never as a live path."""
    mode = str(UNRELATED_PATH_MODE or "off").strip().lower()
    return mode if mode in MODES else "off"


@dataclass(frozen=True)
class PremoveParams:
    cut_pts_at_ref: float = 120.0    # 1m leg zigzag reversal, scaled by px / ref_price
    ref_price: float = 29000.0
    big_pct: float = 1.14            # size / px_0919 * 100 >= big_pct
    history_days: float = 15.0       # 5m window = [D - history_days, 09:20)
    history_slack_days: float = 3.0  # frame must start <= window start + slack, else UNKNOWN
    part_k: float = 0.5              # 5m zigzag threshold = part_k * size
    part_mult: float = 1.25          # big-scale leg >= part_mult * size
    session_open_hours_before: float = 6.0   # D 00:00 - 6h = 18:00 ET prior day
    boundary_hhmm: tuple = (9, 20)
    mid_min_ahead_pts: float = 5.0   # executor fallback floor (§11.5 Q3)
    ticker: str = "MNQ"


DEFAULT_PARAMS = PremoveParams()


def _iso(ts):
    return ts.isoformat() if isinstance(ts, pd.Timestamp) else ts


def _num(x):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else x


@dataclass(frozen=True)
class PremoveContext:
    status: str
    reason: str
    leg_direction: "str | None" = None
    origin: "float | None" = None
    origin_ts: "pd.Timestamp | None" = None
    extreme: "float | None" = None
    extreme_ts: "pd.Timestamp | None" = None
    size: "float | None" = None
    size_pct: "float | None" = None
    px_0919: "float | None" = None
    cut_pts: "float | None" = None
    big_direction: "str | None" = None
    big_origin_ts: "pd.Timestamp | None" = None
    big_extreme_ts: "pd.Timestamp | None" = None
    big_size: "float | None" = None
    boundary: "pd.Timestamp | None" = None
    origin_source: str = "auto"
    auto_leg: "dict | None" = None
    params: "dict | None" = None

    def forced_direction(self) -> "str | None":
        """The opposite of the leg — iff UNRELATED. Anything else forces nothing."""
        if self.status != UNRELATED:
            return None
        return _OPPOSITE.get(str(self.leg_direction or "").upper())

    def mid_at_boundary(self) -> "float | None":
        if self.origin is None or self.extreme is None:
            return None
        return leg_mid(self.origin, self.extreme)

    def to_dict(self) -> dict:
        """JSON-safe: timestamps as isoformat, floats as floats."""
        out = {}
        for k, v in asdict(self).items():
            if isinstance(v, pd.Timestamp):
                v = v.isoformat()
            elif isinstance(v, (np.floating, np.integer)):
                v = float(v)
            out[k] = v
        return out


def _ctx(status, reason, params, **kw) -> PremoveContext:
    return PremoveContext(status=status, reason=reason, params=asdict(params), **kw)


def leg_mid(origin: float, extreme: float) -> float:
    return (float(origin) + float(extreme)) / 2.0


def extend_extreme(leg_direction: str, extreme: float, highs_or_lows) -> float:
    """The leg's extreme extended by `highs_or_lows`: max for an UP leg, min for DOWN.
    NaNs are ignored; an empty sequence returns `extreme` unchanged."""
    vals = [float(v) for v in (highs_or_lows if highs_or_lows is not None else ())
            if v is not None and not (isinstance(v, float) and math.isnan(v))]
    vals = [v for v in vals if not math.isnan(v)]
    if not vals:
        return float(extreme)
    if str(leg_direction).upper() == "UP":
        return max(float(extreme), max(vals))
    return min(float(extreme), min(vals))


def leg_zigzag(frame, thr):
    """Current leg of a high/low zigzag with reversal threshold `thr`. Exact port of
    `studies/premove_mid_retrace/build.leg_zigzag` (lower-case columns).
    Returns (dir, origin, origin_ts, extreme, extreme_ts) or None if no leg confirmed."""
    H, L, T = frame["high"].values, frame["low"].values, frame.index
    if len(H) == 0:
        return None
    trend = None
    hi, hi_i, lo, lo_i = H[0], 0, L[0], 0
    piv = None
    ext = ext_i = None
    for i in range(1, len(H)):
        h, l = H[i], L[i]
        if trend is None:
            if h > hi:
                hi, hi_i = h, i
            if l < lo:
                lo, lo_i = l, i
            if hi - lo >= thr:
                if hi_i > lo_i:
                    trend, piv, ext, ext_i = "UP", (lo, lo_i), hi, hi_i
                else:
                    trend, piv, ext, ext_i = "DOWN", (hi, hi_i), lo, lo_i
            continue
        if trend == "UP":
            if h > ext:
                ext, ext_i = h, i
            elif ext - l >= thr:
                piv = (ext, ext_i)
                trend, ext, ext_i = "DOWN", l, i
        else:
            if l < ext:
                ext, ext_i = l, i
            elif h - ext >= thr:
                piv = (ext, ext_i)
                trend, ext, ext_i = "UP", h, i
    if trend is None:
        return None
    return trend, piv[0], T[piv[1]], ext, T[ext_i]


def pivots_zigzag(frame, thr):
    """All confirmed pivots of a high/low zigzag plus the running extreme:
    [(ts, price, 'H'|'L')]. Exact port of `studies/premove_mid_retrace/context.pivots_zigzag`."""
    H, L, T = frame["high"].values, frame["low"].values, frame.index
    piv = []
    if len(H) == 0:
        return piv
    trend = None
    hi, hi_i, lo, lo_i = H[0], 0, L[0], 0
    ext = ext_i = None
    for i in range(1, len(H)):
        h, l = H[i], L[i]
        if trend is None:
            if h > hi:
                hi, hi_i = h, i
            if l < lo:
                lo, lo_i = l, i
            if hi - lo >= thr:
                if hi_i > lo_i:
                    piv.append((T[lo_i], lo, "L")); trend, ext, ext_i = "UP", hi, hi_i
                else:
                    piv.append((T[hi_i], hi, "H")); trend, ext, ext_i = "DOWN", lo, lo_i
            continue
        if trend == "UP":
            if h > ext:
                ext, ext_i = h, i
            elif ext - l >= thr:
                piv.append((T[ext_i], ext, "H")); trend, ext, ext_i = "DOWN", l, i
        else:
            if l < ext:
                ext, ext_i = l, i
            elif h - ext >= thr:
                piv.append((T[ext_i], ext, "L")); trend, ext, ext_i = "UP", h, i
    if trend:
        piv.append((T[ext_i], ext, "H" if trend == "UP" else "L"))
    return piv


def _normalise(frame) -> "pd.DataFrame | None":
    """Lower-case OHLC, ET index, de-duplicated (keep last), sorted, NaN rows dropped."""
    if not isinstance(frame, pd.DataFrame) or not len(frame):
        return None
    df = frame.rename(columns=lambda c: str(c).lower())
    if not {"open", "high", "low", "close"} <= set(df.columns):
        return None
    df = df[["open", "high", "low", "close"]]
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    idx = df.index
    df = df.set_axis(idx.tz_localize(TZ) if idx.tz is None else idx.tz_convert(TZ))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["high", "low", "close"])
    return df if len(df) else None


def _operator_leg(pre, d_norm, leg_dir, hhmm):
    """The labelling page's customLeg: origin at the operator's minute, extreme re-taken
    from that bar to 09:19 in `leg_dir`. None when the minute has no bar."""
    hh, mm = (int(x) for x in str(hhmm).split(":"))
    day = d_norm - pd.Timedelta(days=1) if hh >= 18 else d_norm
    ts = day + pd.Timedelta(hours=hh, minutes=mm)
    if ts not in pre.index:
        return None
    seg = pre.loc[ts:]
    if leg_dir == "UP":
        origin = float(pre.at[ts, "low"])
        e_ts = seg["high"].idxmax()
        extreme = float(seg["high"].max())
    else:
        origin = float(pre.at[ts, "high"])
        e_ts = seg["low"].idxmin()
        extreme = float(seg["low"].min())
    return leg_dir, origin, ts, extreme, e_ts


def classify(mnq_1m, now, params: "PremoveParams | None" = None) -> PremoveContext:
    """The pre-09:20 context of `now`'s session. Never raises (see the module docstring).

    `params=None` reads the module's `DEFAULT_PARAMS` at CALL time, so a harness can swap
    it between replays (the plan's `params=DEFAULT_PARAMS` default would bind at import)."""
    params = params or DEFAULT_PARAMS
    try:
        return _classify(mnq_1m, now, params)
    except Exception as exc:                                  # total by contract
        return _ctx(UNKNOWN, f"classifier error: {type(exc).__name__}: {exc}", params)


def _classify(mnq_1m, now, params: PremoveParams) -> PremoveContext:
    if now is None:
        return _ctx(UNKNOWN, "no bar time", params)
    now = pd.to_datetime(now)
    now = now.tz_localize(TZ) if now.tzinfo is None else now.tz_convert(TZ)
    d_norm = now.normalize()
    bh, bm = params.boundary_hhmm
    boundary = d_norm + pd.Timedelta(hours=bh, minutes=bm)
    if now < boundary:
        return _ctx(UNKNOWN, "armed before boundary", params, boundary=boundary)

    frame = _normalise(mnq_1m)
    if frame is None:
        return _ctx(UNKNOWN, "no usable MNQ 1m frame (empty or missing OHLC columns)",
                    params, boundary=boundary)

    sess_open = d_norm - pd.Timedelta(hours=params.session_open_hours_before)
    pre = frame[(frame.index >= sess_open) & (frame.index < boundary)]
    last_min = boundary - pd.Timedelta(minutes=1)
    if last_min not in pre.index:
        return _ctx(UNKNOWN, f"no {last_min.strftime('%H:%M')} bar", params,
                    boundary=boundary)
    px = float(pre["close"].iloc[-1])
    cut = params.cut_pts_at_ref * px / params.ref_price
    z = leg_zigzag(pre, cut)
    if z is None:
        return _ctx(NO_LEG, f"no {cut:.2f}-pt zigzag leg confirmed since the session open",
                    params, px_0919=px, cut_pts=cut, boundary=boundary)

    origin_source, auto_leg = "auto", None
    override = LEG_START_OVERRIDES.get(d_norm.date().isoformat()) if LEG_START_OVERRIDES else None
    if override:
        op = _operator_leg(pre, d_norm, z[0], override)
        if op is not None:
            auto_leg = {"leg_direction": z[0], "origin": float(z[1]), "origin_ts": _iso(z[2]),
                        "extreme": float(z[3]), "extreme_ts": _iso(z[4]),
                        "size": abs(float(z[3]) - float(z[1])), "operator_leg_start": override}
            z, origin_source = op, "operator"
        else:
            auto_leg = {"operator_leg_start": override, "ignored": "no bar at that minute"}

    leg_dir, origin, origin_ts, extreme, extreme_ts = z
    origin, extreme = float(origin), float(extreme)
    size = abs(extreme - origin)
    size_pct = size / px * 100.0
    leg = dict(leg_direction=leg_dir, origin=origin, origin_ts=origin_ts, extreme=extreme,
               extreme_ts=extreme_ts, size=size, size_pct=size_pct, px_0919=px,
               cut_pts=cut, boundary=boundary, origin_source=origin_source,
               auto_leg=auto_leg)
    if size_pct < params.big_pct:
        return _ctx(NOT_BIG, f"leg {leg_dir} {size:.2f} pts = {size_pct:.3f}% < "
                    f"{params.big_pct}%", params, **leg)

    win_start = d_norm - pd.Timedelta(days=params.history_days)
    if frame.index[0] > win_start + pd.Timedelta(days=params.history_slack_days):
        have = (boundary - frame.index[0]).total_seconds() / 86400.0
        return _ctx(UNKNOWN, f"history {have:.1f} d < {params.history_days:g} d "
                    f"(frame starts {frame.index[0].isoformat()})", params, **leg)
    hist = frame[(frame.index >= win_start) & (frame.index < boundary)]
    hist5 = hist.resample("5min").agg({"open": "first", "high": "max", "low": "min",
                                       "close": "last"}).dropna()
    piv = pivots_zigzag(hist5, params.part_k * size)
    if len(piv) < 3:
        return _ctx(UNKNOWN, f"{len(piv)} big-scale pivots (< 3)", params, **leg)
    cur_o, cur_e = piv[-2], piv[-1]
    cur_up = cur_e[1] > cur_o[1]
    cur_size = abs(float(cur_e[1]) - float(cur_o[1]))
    big = dict(big_direction="UP" if cur_up else "DOWN", big_origin_ts=cur_o[0],
               big_extreme_ts=cur_e[0], big_size=cur_size)
    same_dir = cur_up == (leg_dir == "UP")
    earlier = cur_o[0] < sess_open
    bigger = cur_size >= params.part_mult * size
    if same_dir and earlier and bigger:
        return _ctx(PART, f"big-scale {big['big_direction']} {cur_size:.2f} pts from "
                    f"{cur_o[0].isoformat()} contains the {size:.2f}-pt leg", params,
                    **leg, **big)
    why = ("opposite direction" if not same_dir else
           "starts in today's session" if not earlier else
           f"{cur_size:.2f} < {params.part_mult} x {size:.2f}")
    return _ctx(UNRELATED, f"big leg {leg_dir} {size:.2f} pts not part of a bigger move "
                f"({why})", params, **leg, **big)
