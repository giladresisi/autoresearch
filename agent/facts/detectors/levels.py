"""Level extraction plus sweep / depletion / supersession / gap-traversal state. Pure.

Windows are CME sessions (18:00 ET -> 17:00 ET the next day), addressed by the repo's
`trade_date` convention (`derive_facts.trade_date`: `(ts + 7h).date()`). `week_start_ts`
reproduces the LIVE anchoring (`session_pipeline._week_start_ts`): a Monday session
anchors to the previous Thursday 18:00, a Tuesday session to the previous Friday 18:00,
everything else to Sunday 18:00. (This is deliberately NOT `derive_facts.week_start_ts`,
which anchors one day earlier for the L1-thesis-only copy.)

Every level carries BOTH the wick extreme (`price`) and the body/close extreme
(`extra["body_price"]`) — thesis.md's P1/P2 criteria turn on the distinction.
"""
from __future__ import annotations

import datetime
import re

import pandas as pd

from agent.facts.detectors._common import normalize, truncate
from agent.facts.ids import fact_id, fact_label
from agent.facts.records import Fact, FactClass, FactState

TZ = "America/New_York"
TRADE_DATE_OFFSET = pd.Timedelta(hours=7)

# A session must carry at least this many bars to mint prevN levels. Guards against
# stray ticks minting phantom prev-days (the POC run-3 G12 failure). Deliberately
# LOWER than derive_facts' 1000 — that constant assumes 1s/1m full-session frames,
# while this detector is also driven over short incremental slices.
MIN_SESSION_BARS = 200

MAX_PREV_DAYS = 3

# Depletion thresholds, mirroring smt_detect.DEPLETE_PTS_* (pts past the level).
DEPLETE_PTS = {
    "MNQ": {"week": 80.0, "day": 40.0, "session": 20.0},
    "MES": {"week": 12.0, "day": 6.0, "session": 3.0},
}

_PREV_LEVEL_RE = re.compile(r"^prev(\d+)_(day|week)_(high|low)$")


def trade_date(ts: pd.Timestamp) -> datetime.date:
    return (ts + TRADE_DATE_OFFSET).date()


def week_start_ts(now: pd.Timestamp) -> pd.Timestamp:
    """Live week anchor: Sun 18:00 ET, extended for early-week sessions (Monday session
    -> prev Thursday 18:00, Tuesday session -> prev Friday 18:00)."""
    today = now.date()
    session_open = today if now.hour >= 18 else today - datetime.timedelta(days=1)
    wd = session_open.weekday()                    # Mon=0 .. Sun=6
    if wd == 6:        # Sunday open -> Monday session
        anchor = session_open - datetime.timedelta(days=3)      # prev Thursday
    elif wd == 0:      # Monday open -> Tuesday session
        anchor = session_open - datetime.timedelta(days=3)      # prev Friday
    else:
        anchor = session_open - datetime.timedelta(days=(wd + 1) % 7)
    return pd.Timestamp(datetime.datetime(anchor.year, anchor.month, anchor.day, 18, 0),
                        tz=TZ)


def _extreme_row(frame: pd.DataFrame, kind: str) -> "tuple[float, float, pd.Timestamp] | None":
    """(wick_price, body_price, ts) for the window's high or low."""
    if len(frame) == 0:
        return None
    if kind == "high":
        ts = frame["High"].idxmax()
        return float(frame["High"].max()), float(frame["Close"].max()), ts
    ts = frame["Low"].idxmin()
    return float(frame["Low"].min()), float(frame["Close"].min()), ts


def _mk_level(ticker: str, name: str, tier: str, kind: str, window_start, window_end,
              wick: float, body: float, ts: pd.Timestamp, now_price) -> Fact:
    fid = fact_id(FactClass.LEVEL, ticker, tier=tier, kind=kind,
                  window_start=window_start, window_end=window_end, price=wick)
    side = None
    if now_price is not None:
        side = "above" if wick >= now_price else "below"
    f = Fact(
        id=fid, cls=FactClass.LEVEL, ticker=ticker, label="", name=name,
        reference_ts=ts, price=wick, price_low=None, price_high=None,
        timeframe=None, resolution="1min", state=FactState.LIVE, state_ts=ts,
        provenance={"detector": "levels", "window_start": window_start,
                    "window_end": window_end},
        extra={"tier": tier, "kind": kind, "body_price": body, "side": side},
    )
    f.label = fact_label(f)
    return f


def detect_levels(hist: pd.DataFrame, now: pd.Timestamp, ticker: str) -> "list[Fact]":
    """prevN day + prev1 week + current-session day extremes, as of `now`.

    Total: an empty / malformed frame yields `[]`, never an exception.
    """
    df = truncate(normalize(hist), now)
    if len(df) == 0:
        return []

    tds = pd.Series((df.index + TRADE_DATE_OFFSET).date, index=df.index)
    td_now = trade_date(now)
    counts = tds.value_counts()
    usable = sorted(d for d, n in counts.items() if n >= MIN_SESSION_BARS or d == td_now)
    past = [d for d in usable if d < td_now]
    now_price = float(df["Close"].iloc[-1])

    out: list[Fact] = []

    # ---- prevN day levels (most recent = prev1) ---------------------------- #
    for n, d in enumerate(reversed(past[-MAX_PREV_DAYS:]), start=1):
        frame = df[tds.to_numpy() == d]
        if len(frame) == 0:
            continue
        w0, w1 = frame.index[0], frame.index[-1]
        for kind in ("high", "low"):
            got = _extreme_row(frame, kind)
            if got is None:
                continue
            wick, body, ts = got
            out.append(_mk_level(ticker, f"prev{n}_day_{kind}", "day", kind,
                                 w0, w1, wick, body, ts, now_price))

    # ---- prev1 week levels ------------------------------------------------- #
    wk_start = week_start_ts(now)
    prev_wk_start = wk_start - pd.Timedelta(days=7)
    prev_wk = df[(df.index >= prev_wk_start) & (df.index < wk_start)]
    if len(prev_wk):
        for kind in ("high", "low"):
            got = _extreme_row(prev_wk, kind)
            if got is None:
                continue
            wick, body, ts = got
            out.append(_mk_level(ticker, f"prev1_week_{kind}", "week", kind,
                                 prev_wk_start, wk_start, wick, body, ts, now_price))

    # ---- current session running extremes ---------------------------------- #
    cur = df[tds.to_numpy() == td_now]
    if len(cur):
        w0, w1 = cur.index[0], cur.index[-1]
        for kind in ("high", "low"):
            got = _extreme_row(cur, kind)
            if got is None:
                continue
            wick, body, ts = got
            out.append(_mk_level(ticker, f"day_{kind}", "session", kind,
                                 w0, w1, wick, body, ts, now_price))

    return out


def apply_supersession(levels: "list[Fact]") -> "list[Fact]":
    """thesis.md §2.1b: within a same-ticker/tier/side prevN family, a level is NESTED
    (SUPERSEDED) when some MORE RECENT (smaller N) level in that family already extends
    at least as far. Pure price comparison over the full tracked depth.

    Re-runnable over the WHOLE set after any insert — adding a deeper level can
    reclassify levels already held, so this never does incremental bookkeeping.
    """
    if not levels:
        return list(levels or [])

    families: dict = {}
    for f in levels:
        if f.cls is not FactClass.LEVEL or f.price is None:
            continue
        m = _PREV_LEVEL_RE.match(f.name or "")
        if not m:
            continue
        n, tier, kind = int(m.group(1)), m.group(2), m.group(3)
        families.setdefault((f.ticker, tier, kind), {})[n] = (f.price, f)

    for (_tkr, _tier, kind), by_n in families.items():
        order = sorted(by_n)                                # 1 = most recent
        for i, n in enumerate(order):
            price_n, fact_n = by_n[n]
            more_recent = [by_n[m][0] for m in order[:i]]
            nested = (any(p <= price_n for p in more_recent) if kind == "low"
                      else any(p >= price_n for p in more_recent))
            if nested and fact_n.state is FactState.LIVE:
                fact_n.set_state(FactState.SUPERSEDED, fact_n.state_ts)
    return list(levels)


def apply_gap_traversal(levels: "list[Fact]", prev_close: float, reopen: float,
                        ts: pd.Timestamp) -> "list[Fact]":
    """A reopen that gaps PAST a level is not a stop-run — no wick, no rejection, no
    liquidity taken. Levels strictly between `prev_close` and `reopen` become
    TRAVERSED_BY_GAP, never SWEPT."""
    lo, hi = (prev_close, reopen) if prev_close <= reopen else (reopen, prev_close)
    for f in levels or ():
        if f.cls is not FactClass.LEVEL or f.price is None:
            continue
        if lo < f.price < hi and f.state in (FactState.LIVE, FactState.SUPERSEDED):
            f.set_state(FactState.TRAVERSED_BY_GAP, ts)
    return list(levels or [])


def apply_sweep_states(levels: "list[Fact]", bars: pd.DataFrame,
                       now: "pd.Timestamp | None" = None) -> "list[Fact]":
    """Evaluate each level forward from the end of its own window to `now`.

    A wick through the level marks it SWEPT; running past it by the tier's depletion
    threshold marks it DEPLETED. SUPERSEDED / TRAVERSED_BY_GAP are terminal here —
    supersession is a statement about the level universe, not about price action.
    """
    df = truncate(normalize(bars), now)
    if len(df) == 0 or not levels:
        return list(levels or [])

    for f in levels:
        if f.cls is not FactClass.LEVEL or f.price is None:
            continue
        if f.state in (FactState.SUPERSEDED, FactState.TRAVERSED_BY_GAP):
            continue
        w1 = (f.provenance or {}).get("window_end")
        after = df[df.index > w1] if isinstance(w1, pd.Timestamp) else df
        if len(after) == 0:
            continue
        kind = f.extra.get("kind")
        thr = DEPLETE_PTS.get(f.ticker, {}).get(f.extra.get("tier"), None)
        if kind == "high":
            hit = after[after["High"] >= f.price]
            if len(hit) == 0:
                continue
            f.set_state(FactState.SWEPT, hit.index[0])
            if thr is not None and float(after["High"].max()) >= f.price + thr:
                past = after[after["High"] >= f.price + thr]
                f.set_state(FactState.DEPLETED, past.index[0])
        else:
            hit = after[after["Low"] <= f.price]
            if len(hit) == 0:
                continue
            f.set_state(FactState.SWEPT, hit.index[0])
            if thr is not None and float(after["Low"].min()) <= f.price - thr:
                past = after[after["Low"] <= f.price - thr]
                f.set_state(FactState.DEPLETED, past.index[0])
    return list(levels)
