"""Unnested weekly / monthly extremes as T2 target pools (plan 40, `l2-target-selection.md` §7a).

PURE: no `paths`, no clock, no I/O. The disk loader is `agent/facts/htf_source.py`; this
module only turns a 1m OHLC frame into a list of levels, and it reads no bar at or after
the instant it is asked about.

**What a level is.** For every COMPLETED ISO week and calendar month (both keyed on the
TRADE date, `ts + 7h`, the `derive_facts._long_horizon_extremes` convention) since the
per-asset `EXTREMES_START`, the period's high and low. A level is DROPPED once any later
bar trades STRICTLY beyond it (an equal print does not nest it). What survives is a
frontier: in time order the highs strictly decrease and the lows strictly increase.

**The running week and month (Q5, operator 2026-09-22).** The in-progress week and month
are not in the completed list. Their extremes are `running_extremes_at`: the part before
the session open (`running_period_seed`, fixed at the open from the same 1m file) combined
with today's bars up to the fill. They pass the same draw floor as every pool, so an
extreme price is still making sits inside the floor and is filtered out by the menu.

**Dedupe (Q3).** Equal prices on one side collapse to one row: month over week, then the
most recent; the others are kept as `aliases`.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TZ = "America/New_York"

#: The ONE per-asset constant (operator, 2026-09-22). A TRADE date: bars whose trade date
#: is on or after it count, so the week that contains the start is truncated, not dropped.
EXTREMES_START = {
    "MNQ": pd.Timestamp("2026-06-16 00:00", tz=TZ),   # MNQ ATH (chart); includes the Jul 29 low
    "MES": pd.Timestamp("2026-06-11 00:00", tz=TZ),   # MES lowest low since May 5; includes the Aug 13 ATH
}

#: Trade date = ts + 7h (an 18:00 ET bar belongs to the next calendar day's session).
TRADE_DATE_SHIFT = pd.Timedelta(hours=7)

#: `assemble.drop_maintenance`'s exact inclusive bounds.
_MAINT_LO_S = 16 * 3600 + 55 * 60          # 16:55:00 kept
_MAINT_HI_S = 18 * 3600                    # 18:00:00 kept

TIER_WEEK, TIER_MONTH = "htf_week", "htf_month"
TIER_WEEK_RUNNING, TIER_MONTH_RUNNING = "htf_week_running", "htf_month_running"
TIERS = (TIER_WEEK, TIER_MONTH, TIER_WEEK_RUNNING, TIER_MONTH_RUNNING)
#: Dedupe precedence: a month outranks a week (running or completed).
_TIER_RANK = {TIER_WEEK: 0, TIER_WEEK_RUNNING: 0, TIER_MONTH: 1, TIER_MONTH_RUNNING: 1}

_CANON = {"open": "Open", "high": "High", "low": "Low", "close": "Close",
          "volume": "Volume"}


@dataclass(frozen=True)
class HtfExtreme:
    name: str
    ticker: str
    price: float
    side: str                     # "above" (a high) | "below" (a low)
    tier: str                     # one of TIERS
    period: str                   # "2026-W34" | "2026-08"
    ts: pd.Timestamp              # the bar that printed the extreme
    aliases: tuple = field(default_factory=tuple)
    is_window_extreme: bool = False

    def to_json(self) -> dict:
        return {"name": self.name, "ticker": self.ticker, "price": self.price,
                "side": self.side, "tier": self.tier, "period": self.period,
                "ts": self.ts.isoformat(), "aliases": list(self.aliases),
                "is_window_extreme": self.is_window_extreme}

    @classmethod
    def from_json(cls, d: dict) -> "HtfExtreme":
        return cls(name=d["name"], ticker=d["ticker"], price=float(d["price"]),
                   side=d["side"], tier=d["tier"], period=d["period"],
                   ts=pd.Timestamp(d["ts"]).tz_convert(TZ),
                   aliases=tuple(d.get("aliases") or ()),
                   is_window_extreme=bool(d.get("is_window_extreme")))


# --------------------------------------------------------------------------- #
# frame preparation                                                             #
# --------------------------------------------------------------------------- #

def session_as_of(trade_date) -> pd.Timestamp:
    """The session open of `trade_date`: (trade_date - 1 day) 18:00 ET."""
    d = pd.Timestamp(str(trade_date)[:10]).date()
    return pd.Timestamp(datetime.datetime.combine(d - datetime.timedelta(days=1),
                                                  datetime.time(18, 0)), tz=TZ)


def trade_date_of(ts: pd.Timestamp) -> datetime.date:
    return (pd.Timestamp(ts).tz_convert(TZ) + TRADE_DATE_SHIFT).date()


def _ohlc(frame) -> pd.DataFrame:
    """Capital-cased High/Low, ET index, sorted, maintenance bars dropped. Empty on
    degenerate input — never raises."""
    empty = pd.DataFrame(columns=["High", "Low"],
                         index=pd.DatetimeIndex([], tz=TZ))
    if frame is None or not len(frame) or not isinstance(frame.index, pd.DatetimeIndex):
        return empty
    ren = {c: _CANON[str(c).lower()] for c in frame.columns
           if str(c).lower() in _CANON and c != _CANON[str(c).lower()]}
    df = frame.rename(columns=ren) if ren else frame
    if "High" not in df.columns or "Low" not in df.columns:
        return empty
    df = df[["High", "Low"]].dropna()
    idx = df.index
    if idx.tz is None:
        df = df.set_axis(idx.tz_localize(TZ))
    elif str(idx.tz) != TZ:
        df = df.set_axis(idx.tz_convert(TZ))
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()
    ix = df.index
    sod = (ix.hour.to_numpy() * 3600 + ix.minute.to_numpy() * 60 + ix.second.to_numpy()
           + ix.microsecond.to_numpy() / 1e6)
    keep = (sod <= _MAINT_LO_S) | (sod >= _MAINT_HI_S)
    return df[keep]


def _window(frame, lo: "pd.Timestamp | None", hi: pd.Timestamp) -> pd.DataFrame:
    """Rows with lo <= ts < hi. The coarse cut is taken on the raw frame first so a
    multi-year file is not normalised row by row."""
    if frame is None or not len(frame) or not isinstance(frame.index, pd.DatetimeIndex):
        return _ohlc(None)
    idx = frame.index
    hi_c = hi if idx.tz is not None else hi.tz_localize(None)
    sl = frame[idx < hi_c]
    if lo is not None:
        lo_c = lo if idx.tz is not None else lo.tz_localize(None)
        sl = sl[sl.index >= lo_c]
    return _ohlc(sl)


def _start_bound(start: pd.Timestamp) -> pd.Timestamp:
    """First ts whose trade date is >= start's date."""
    return pd.Timestamp(start).tz_convert(TZ).normalize() - TRADE_DATE_SHIFT


def _keys(index: pd.DatetimeIndex):
    """(trade dates, ISO week keys, month keys) per row."""
    td = (index + TRADE_DATE_SHIFT).normalize().tz_localize(None)
    iso = td.isocalendar()
    week = (iso["year"].to_numpy().astype(np.int64) * 100
            + iso["week"].to_numpy().astype(np.int64))
    month = td.year.to_numpy().astype(np.int64) * 100 + td.month.to_numpy().astype(np.int64)
    return td, week, month


def _week_label(key: int) -> str:
    return f"{key // 100}-W{key % 100:02d}"


def _month_label(key: int) -> str:
    return f"{key // 100}-{key % 100:02d}"


def _resolve_start(ticker: str, start) -> pd.Timestamp:
    if start is not None:
        return pd.Timestamp(start) if pd.Timestamp(start).tz is not None \
            else pd.Timestamp(start).tz_localize(TZ)
    try:
        return EXTREMES_START[ticker]
    except KeyError:
        raise ValueError(f"no EXTREMES_START for {ticker!r}") from None


# --------------------------------------------------------------------------- #
# dedupe                                                                        #
# --------------------------------------------------------------------------- #

def merge_extremes(rows) -> "list[HtfExtreme]":
    """Q3: one row per (side, price). Keeper = month over week, then most recent; the
    others' names (and their own aliases) become the keeper's aliases. `is_window_extreme`
    is recomputed over the result: the max high and the min low."""
    groups: dict = {}
    for r in rows:
        groups.setdefault((r.side, float(r.price)), []).append(r)
    out = []
    for (_side, _price), grp in groups.items():
        grp = sorted(grp, key=lambda r: (_TIER_RANK.get(r.tier, 0), r.ts), reverse=True)
        keep = grp[0]
        aliases = set(keep.aliases)
        for other in grp[1:]:
            aliases.add(other.name)
            aliases.update(other.aliases)
        aliases.discard(keep.name)
        out.append((keep, tuple(sorted(aliases))))
    highs = [k.price for k, _a in out if k.side == "above"]
    lows = [k.price for k, _a in out if k.side == "below"]
    hmax = max(highs) if highs else None
    lmin = min(lows) if lows else None
    final = []
    for keep, aliases in out:
        wx = ((keep.side == "above" and keep.price == hmax)
              or (keep.side == "below" and keep.price == lmin))
        final.append(HtfExtreme(keep.name, keep.ticker, keep.price, keep.side, keep.tier,
                                keep.period, keep.ts, aliases, bool(wx)))
    final.sort(key=lambda r: (r.side, r.ts, r.name))
    return final


# --------------------------------------------------------------------------- #
# the completed list                                                            #
# --------------------------------------------------------------------------- #

def compute_unnested_extremes(frame, as_of, *, ticker, start=None) -> "list[HtfExtreme]":
    """The unnested extremes of every COMPLETED week and month, as of `as_of`.

    Rows used: trade date >= start, ts < as_of, maintenance dropped. The running week and
    month (the ones `as_of`'s trade date belongs to) are excluded — they are
    `running_extremes_at`'s. Every later bar before `as_of`, the running period's
    included, can nest a level."""
    as_of = pd.Timestamp(as_of).tz_convert(TZ)
    start = _resolve_start(ticker, start)
    df = _window(frame, _start_bound(start), as_of)
    if not len(df):
        return []
    td, week, month = _keys(df.index)
    cur = pd.Timestamp(trade_date_of(as_of))
    cur_week = int(cur.isocalendar()[0]) * 100 + int(cur.isocalendar()[1])
    cur_month = cur.year * 100 + cur.month

    hi = df["High"].to_numpy(dtype=float)
    lo = df["Low"].to_numpy(dtype=float)
    n = len(df)
    # Strictly-after suffix extremes: after_hi[i] = max(hi[i+1:]), -inf for the last row.
    after_hi = np.empty(n)
    after_lo = np.empty(n)
    after_hi[:-1] = np.maximum.accumulate(hi[::-1])[::-1][1:]
    after_lo[:-1] = np.minimum.accumulate(lo[::-1])[::-1][1:]
    after_hi[-1] = -np.inf
    after_lo[-1] = np.inf
    pos = np.arange(n)

    rows = []
    for tier, keys, cur_key, label in ((TIER_WEEK, week, cur_week, _week_label),
                                       (TIER_MONTH, month, cur_month, _month_label)):
        s_hi = pd.Series(hi, index=pos)
        s_lo = pd.Series(lo, index=pos)
        g_keys = pd.Series(keys, index=pos)
        done = g_keys != cur_key
        if not done.any():
            continue
        # First occurrence of the period max / min (idxmax/idxmin are first-hit).
        i_hi = s_hi[done].groupby(g_keys[done]).idxmax()
        i_lo = s_lo[done].groupby(g_keys[done]).idxmin()
        for key, i in i_hi.items():
            price = float(hi[i])
            if after_hi[i] > price:
                continue                               # traded strictly beyond later
            rows.append(_row(tier, "high", key, label, i, price, df.index, td, ticker))
        for key, i in i_lo.items():
            price = float(lo[i])
            if after_lo[i] < price:
                continue
            rows.append(_row(tier, "low", key, label, i, price, df.index, td, ticker))
    return merge_extremes(rows)


def _row(tier, word, key, label, i, price, index, td, ticker) -> HtfExtreme:
    ts = index[i]
    if tier == TIER_WEEK:
        name = f"htf_week_{word}_{td[i]:%Y%m%d}"
    else:
        name = f"htf_month_{word}_{key}"
    return HtfExtreme(name=name, ticker=ticker, price=price,
                      side="above" if word == "high" else "below", tier=tier,
                      period=label(int(key)), ts=ts)


# --------------------------------------------------------------------------- #
# the running week / month                                                      #
# --------------------------------------------------------------------------- #

def running_period_seed(frame, as_of, *, ticker, start=None) -> dict:
    """The running week's and month's extremes over [period start, as_of), from the same
    1m frame as the completed list, fixed at the session open.

    {"trade_date", "week": {"period", "high": (price, ts) | None, "low": ...},
     "month": {...}}. A part is None when the period has no bar before `as_of` (a Monday
    session for the week, the month's first session for the month)."""
    as_of = pd.Timestamp(as_of).tz_convert(TZ)
    start = _resolve_start(ticker, start)
    cur = pd.Timestamp(trade_date_of(as_of))
    iso = cur.isocalendar()
    cur_week = int(iso[0]) * 100 + int(iso[1])
    cur_month = cur.year * 100 + cur.month
    out = {"ticker": ticker, "trade_date": cur.strftime("%Y-%m-%d"),
           "week": {"period": _week_label(cur_week), "high": None, "low": None},
           "month": {"period": _month_label(cur_month), "high": None, "low": None}}
    # A month is at most ~23 sessions; 40 days back bounds the scan.
    lo_bound = max(_start_bound(start), as_of - pd.Timedelta(days=40))
    df = _window(frame, lo_bound, as_of)
    if not len(df):
        return out
    _td, week, month = _keys(df.index)
    for part, keys, key in (("week", week, cur_week), ("month", month, cur_month)):
        seg = df[keys == key]
        if not len(seg):
            continue
        out[part]["high"] = _first_extreme(seg, "High", np.argmax)
        out[part]["low"] = _first_extreme(seg, "Low", np.argmin)
    return out


def _first_extreme(seg: pd.DataFrame, col: str, arg) -> tuple:
    """(price, ts) of the FIRST bar printing the column's extreme."""
    a = seg[col].to_numpy(dtype=float)
    i = int(arg(a))
    return float(a[i]), seg.index[i]


def running_extremes_at(seed, bars_today, as_of, now) -> "list[HtfExtreme]":
    """The running week / month high and low at `now`: the seed combined with today's
    bars in [as_of, now). Rows are unnested by definition; deduped per Q3."""
    if not seed:
        return []
    as_of = pd.Timestamp(as_of).tz_convert(TZ)
    now = pd.Timestamp(now).tz_convert(TZ)
    ticker = seed.get("ticker") or "MNQ"
    seg = _window(bars_today, as_of, now)
    t_hi = t_lo = None
    if len(seg):
        t_hi = _first_extreme(seg, "High", np.argmax)
        t_lo = _first_extreme(seg, "Low", np.argmin)
    rows = []
    for part, tier in (("week", TIER_WEEK_RUNNING), ("month", TIER_MONTH_RUNNING)):
        p = seed.get(part) or {}
        for word, today, better in (("high", t_hi, lambda a, b: a > b),
                                    ("low", t_lo, lambda a, b: a < b)):
            prior = p.get(word)
            best = prior
            if today is not None and (best is None or better(today[0], best[0])):
                best = today
            if best is None:
                continue
            rows.append(HtfExtreme(
                name=f"{tier}_{word}", ticker=ticker, price=float(best[0]),
                side="above" if word == "high" else "below", tier=tier,
                period=str(p.get("period") or ""), ts=pd.Timestamp(best[1]).tz_convert(TZ)))
    return merge_extremes(rows)


# --------------------------------------------------------------------------- #
# intraday pruning and the T2 pool set                                          #
# --------------------------------------------------------------------------- #

def still_unnested(extremes, frame, as_of, now) -> "list[HtfExtreme]":
    """Drop any level a bar with as_of <= ts < now has traded strictly beyond."""
    if not extremes:
        return []
    seg = _window(frame, pd.Timestamp(as_of).tz_convert(TZ), pd.Timestamp(now).tz_convert(TZ))
    if not len(seg):
        return list(extremes)
    hi = float(seg["High"].to_numpy().max())
    lo = float(seg["Low"].to_numpy().min())
    return [e for e in extremes
            if not ((e.side == "above" and hi > e.price)
                    or (e.side == "below" and lo < e.price))]


def pools_at(htf: dict, frame, now) -> "list[HtfExtreme]":
    """The HTF rows a T2 menu built at `now` may use: the completed list pruned by today's
    bars, plus the running week / month, merged per Q3.

    `htf` = {"as_of", "extremes": [HtfExtreme], "seed": running_period_seed(...)}."""
    if not htf:
        return []
    as_of = htf["as_of"]
    rows = still_unnested(htf.get("extremes") or (), frame, as_of, now)
    rows = rows + running_extremes_at(htf.get("seed"), frame, as_of, now)
    return merge_extremes(rows)


def menu_rows(extremes) -> list:
    """`derive_facts._dol_menu`'s `extra_pools` shape: (name, price, body, tier, side)."""
    return [(e.name, float(e.price), None, e.tier, e.side) for e in extremes]


def seed_to_json(seed: "dict | None") -> "dict | None":
    if seed is None:
        return None
    out = {"ticker": seed.get("ticker"), "trade_date": seed.get("trade_date")}
    for part in ("week", "month"):
        p = seed.get(part) or {}
        out[part] = {"period": p.get("period")}
        for word in ("high", "low"):
            v = p.get(word)
            out[part][word] = (None if v is None
                               else [float(v[0]), pd.Timestamp(v[1]).isoformat()])
    return out


def seed_from_json(d: "dict | None") -> "dict | None":
    if d is None:
        return None
    out = {"ticker": d.get("ticker"), "trade_date": d.get("trade_date")}
    for part in ("week", "month"):
        p = d.get(part) or {}
        out[part] = {"period": p.get("period")}
        for word in ("high", "low"):
            v = p.get(word)
            out[part][word] = (None if v is None
                               else (float(v[0]), pd.Timestamp(v[1]).tz_convert(TZ)))
    return out
