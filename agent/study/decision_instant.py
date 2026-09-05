"""Cycle 4 plan 30: the pool stack as production would see it.

Cycle 4 measured every distance from the **move origin** — the last counter-direction extreme
the move never exceeded — which is knowable only after the move is over.
`derive_facts._dol_menu` measures `abs(price - now_price)` from the price at the call. Step 0
of the change protocol rejected B9 on exactly that mismatch, so this module rebuilds the stack
at an instant production can actually compute and lets the frozen families be re-scored in
coordinates that port.

**A fixed clock instant is the replacement, and it is the only honest one.** Anything derived
from the move's own shape reintroduces the lookahead. The Executor arms at 09:20 and the move
overwhelmingly starts at the open — 54 of 87 primaries begin in the 09:30 bin, 83 of 87 within
fifteen minutes — so a clock instant shortly after the open sits close to the structural anchor
on most sessions while remaining computable on all of them.

**This is not a rescale.** At a later instant the stack itself is different: 13% of MNQ draws
and 30% of MES draws are already swept by +2 minutes, and a swept pool is not a candidate. The
universe is rebuilt at the boundary, not re-measured from the old one.

What deliberately does NOT change: the direction (spec §1.1 assumes it given), the label (the
phase-2 draw), and the families (frozen at plan 29 §B.2). Only the anchor moves — so a change
in the scores is a fact about the anchor rather than about a new search.
"""
from __future__ import annotations

import datetime

import pandas as pd

from agent.study.candidates import universe
from agent.study.hazard import PoolObs

TZ = "America/New_York"

#: Clock instants tested. Reported separately, never pooled — each is a different decision
#: latency and they are not interchangeable.
INSTANTS = ("09:32", "09:35", "09:40")


def instant_ts(date, hhmm: str) -> pd.Timestamp:
    """`date` + wall time, in ET. `date` may be a `date` or an ISO string."""
    d = datetime.date.fromisoformat(date) if isinstance(date, str) else date
    return pd.Timestamp(f"{d} {hhmm}", tz=TZ)


def _now_price(bars, ts):
    """The last completed 1m close at or before `ts` — what `now_price` means."""
    if bars is None or not len(bars):
        return None
    head = bars.loc[:ts]
    return float(head["close"].iloc[-1]) if len(head) else None


def observations_at(labels, cands, facts, hhmm: str,
                    tickers=("MNQ", "MES")) -> "list[PoolObs]":
    """The stack at `hhmm`, as `PoolObs` records the frozen families already consume.

    `labels` supplies the direction and the draw (the label); `cands` supplies which
    candidate was the draw. Everything geometric is rebuilt from the bundle at the instant.

    A session whose draw has already been swept by `hhmm` yields NO observations and is
    counted by `degenerate_at` — that is a coverage fact, not an absence, and the report
    must show it.
    """
    by_seg: dict = {}
    for lab in labels:
        if lab.get("ticker") in tickers and lab.get("status") == "labelled":
            by_seg.setdefault((lab["date"], lab["segment"]), {})[lab["ticker"]] = lab

    draw_price: dict = {}
    for r in cands:
        if r.get("outcome") == "draw":
            draw_price[(r["date"], r["segment"], r["ticker"])] = r["price"]

    out = []
    for (date, seg), per_tk in by_seg.items():
        ts = instant_ts(date, hhmm)
        try:
            bundle = facts.bundle_at(ts)
        except Exception:
            continue
        pools = universe(bundle, tickers=tuple(per_tk))
        for tk, lab in per_tk.items():
            price_now = _now_price(facts.bars_1m(tk), ts)
            avg = (bundle.avg_range_1h or {}).get(tk)
            target = draw_price.get((date, seg, tk))
            if price_now is None or not avg or target is None:
                continue
            sign = 1 if lab["direction"] == "up" else -1

            # Eligible AT THE INSTANT: ahead of the current price, not already swept.
            live = [c for c in pools
                    if c.ticker == tk and not c.swept_before
                    and (c.price - price_now) * sign > 0]
            live.sort(key=lambda c: abs(c.price - price_now))
            if not live:
                continue
            # The draw must still be ahead, or the decision is degenerate at this instant.
            idx = next((i for i, c in enumerate(live)
                        if abs(c.price - target) < 1e-6), None)
            if idx is None:
                continue

            d = [abs(c.price - price_now) / avg for c in live]
            for j in range(idx + 1):
                out.append(PoolObs(
                    seg=(date, seg), ticker=tk, rank=j, dist=d[j],
                    gap_ahead=(d[j + 1] - d[j]) if j + 1 < len(d) else None,
                    gap_behind=(d[j] - d[j - 1]) if j > 0 else d[j],
                    tier=live[j].tier, cls=live[j].cls, stop=(j == idx),
                    censored=(j + 1 >= len(d)), alt_gap_ahead=None))
    out.sort(key=lambda o: (str(o.seg), o.ticker, o.rank))
    return out


def degenerate_at(labels, cands, facts, hhmm: str,
                  tickers=("MNQ", "MES")) -> dict:
    """`{ticker: {"total": n, "usable": n, "degenerate": n}}`.

    Degenerate = the draw is no longer ahead of price at the instant, because it has been
    swept or price has passed it. The rule cannot name it, and pretending the session is
    simply missing would flatter every accuracy computed afterwards.
    """
    obs = observations_at(labels, cands, facts, hhmm, tickers)
    usable = {tk: {o.seg for o in obs if o.ticker == tk} for tk in tickers}
    out = {}
    for tk in tickers:
        total = {(l["date"], l["segment"]) for l in labels
                 if l.get("ticker") == tk and l.get("status") == "labelled"}
        out[tk] = {"total": len(total), "usable": len(usable[tk]),
                   "degenerate": len(total) - len(usable[tk])}
    return out
