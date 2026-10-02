"""The SMT-wait block (O2, session 2026-10-01) -- `l2-mechanisms.md` §11.6, a CANDIDATE
that is ON by default (operator decision 2026-10-03; `ACT_SMT_WAIT_BLOCK=0` opts out).

Operator, 2026-10-01 11:31 ET: do not enter into a PREDICTABLE opposite SMT. When one
asset is close to the London extreme in the plan's favourable direction and the other is
not, the first post-09:30 sweep of it will be a one-asset sweep, and the entries around it
are the ones that get retraced. This module answers one question -- is a market fire
blocked at `now`? -- and nothing else. The Executor owns the wiring, the records, the flag
and the tmso latch.

Per asset (MNQ, the traded one, and MES), each against its OWN level and its OWN average
1h range; "favourable side" is the lows for a DOWN plan and the highs for an UP one:
- LEVEL: the asset's London extreme on that side, over 00:00:00 <= t < 06:00:00 ET of the
  session date.
- NOT APPLICABLE FOR THE DAY (nothing is ever blocked): either asset traded at or through
  its LEVEL in 06:00:00 <= t < 09:30:00 (the NY morning already took it), either asset has
  no London bars, or either average 1h range is missing / not positive.
- TOUCH: at or after 09:30:00 the asset's Low <= LEVEL (High >= LEVEL for an UP plan).
- WITHIN: distance to LEVEL <= PROXIMITY_RATIO x the asset's average 1h range. MNQ's
  distance is measured from the FIRE's price, MES's from its last Close at or before now.

Phase 1, from 09:30:00 until either asset first touches: BLOCK iff exactly one asset is
WITHIN (reason `smt_wait_proximity`). Both or neither: allow.

Phase 2, once exactly one asset has touched: with B0 the minute floor of the first
touching row, BLOCK every fire with touch_time <= now < B0 + BLOCK_BARS minutes (reason
`smt_wait_sweep`): the intra-bar entries of the sweeping bar and of the next, and the
bar-close entry at the sweeping bar's own close. The first instant allowed is the close of
the next bar. Nothing is blocked once both assets have touched, or after that window.

Pinned readings (implementer's, not individually confirmed by the operator -- §11.6): MNQ's distance from the
fire price and MES's from its last close; trading exactly AT the level is a touch; the
block lifts the instant the second asset touches during phase 2; a touch by both assets in
the same 1s row leaves no phase 2; "not applicable" when either asset took its level
06:00-09:30; inert when either average 1h range is missing.

Pure: no I/O and no clock of its own -- `now` and the frames come from the caller. The
LEVELs and the not-applicable verdict are fixed for the day once 09:30 has passed, so they
are computed once; the touch state advances over the rows added since the last call.
"""
from __future__ import annotations

import pandas as pd

PROXIMITY_RATIO = 0.3
BLOCK_BARS = 2
LONDON_START = (0, 0)
LONDON_END = (6, 0)
RTH_OPEN = (9, 30)

REASON_PROXIMITY = "smt_wait_proximity"
REASON_SWEEP = "smt_wait_sweep"

_SHORT = ("DOWN", "SHORT")
_TICKERS = ("MNQ", "MES")


class _Asset:
    __slots__ = ("level", "scanned", "touch")

    def __init__(self, level: float, scanned: pd.Timestamp) -> None:
        self.level = level
        self.scanned = scanned      # label of the last row looked at for a touch
        self.touch = None           # label of the first touching row


def _has_rows(frame) -> bool:
    return frame is not None and len(frame) > 0


class SmtWait:
    def __init__(self, direction, day: pd.Timestamp) -> None:
        """`day` is the session date at midnight, in the frames' timezone."""
        self._short = str(direction or "").upper() in _SHORT
        self._day = day
        self._open = day + pd.Timedelta(hours=RTH_OPEN[0], minutes=RTH_OPEN[1])
        self._assets = None         # {ticker: _Asset} once applicable
        self._decided = False       # the applicable / not-applicable verdict is cached

    def _at(self, hm) -> pd.Timestamp:
        return self._day + pd.Timedelta(hours=hm[0], minutes=hm[1])

    def _hit(self, frame, level):
        return (frame["Low"] <= level) if self._short else (frame["High"] >= level)

    # -- once per day --------------------------------------------------------- #

    def _decide(self, frames: dict) -> None:
        """Fix the LEVELs, or the day's not-applicable verdict. Needs both frames to hold
        rows (an empty frame is a feed that has not arrived, not a verdict, so it is
        retried on the next call)."""
        if not all(_has_rows(frames.get(t)) for t in _TICKERS):
            return
        start, end = self._at(LONDON_START), self._at(LONDON_END)
        assets = {}
        for t in _TICKERS:
            f = frames[t]
            london = f[(f.index >= start) & (f.index < end)]
            if not len(london):
                self._decided = True
                return
            level = float(london["Low"].min() if self._short else london["High"].max())
            morning = f[(f.index >= end) & (f.index < self._open)]
            if len(morning) and bool(self._hit(morning, level).any()):
                self._decided = True
                return
            assets[t] = _Asset(level, self._open - pd.Timedelta(nanoseconds=1))
        self._assets = assets
        self._decided = True

    # -- every call ------------------------------------------------------------ #

    def _advance(self, asset: _Asset, frame, now) -> None:
        if asset.touch is not None or not _has_rows(frame):
            return
        # From the last scanned row INCLUSIVE: the frame's last row may be an in-progress
        # bar that is still being extended under the same label.
        seg = frame.iloc[frame.index.searchsorted(asset.scanned, side="left"):]
        seg = seg[seg.index <= now]
        if not len(seg):
            return
        hit = self._hit(seg, asset.level).to_numpy()
        if hit.any():
            asset.touch = seg.index[int(hit.argmax())]
        asset.scanned = seg.index[-1]

    def check(self, now, mnq, mes, fire_price, atrs: dict) -> "dict | None":
        """None to allow the fire; else `{"reason", "detail"}` for the veto record.
        `mnq` / `mes` are the frames up to `now`, `atrs` the per-ticker average 1h range."""
        if now < self._open:
            return None
        frames = {"MNQ": mnq, "MES": mes}
        if not self._decided:
            self._decide(frames)
        if self._assets is None:
            return None
        ranges = {t: atrs.get(t) for t in _TICKERS}
        if any(r is None or not float(r) > 0 for r in ranges.values()):
            return None
        for t in _TICKERS:
            self._advance(self._assets[t], frames[t], now)
        if not _has_rows(mes):
            return None
        price = {"MNQ": float(fire_price), "MES": float(mes["Close"].iloc[-1])}
        sign = 1.0 if self._short else -1.0
        level = {t: self._assets[t].level for t in _TICKERS}
        distance = {t: sign * (price[t] - level[t]) for t in _TICKERS}
        ratio = {t: distance[t] / float(ranges[t]) for t in _TICKERS}
        within = {t: ratio[t] <= PROXIMITY_RATIO for t in _TICKERS}
        detail = {"level": level, "price": price,
                  "distance": {t: round(v, 4) for t, v in distance.items()},
                  "avg_range_1h": {t: round(float(ranges[t]), 4) for t in _TICKERS},
                  "ratio": {t: round(v, 4) for t, v in ratio.items()}, "within": within}

        touches = {t: self._assets[t].touch for t in _TICKERS}
        touched = [t for t in _TICKERS if touches[t] is not None]
        if not touched:
            if within["MNQ"] == within["MES"]:
                return None
            detail["phase"] = 1
            return {"reason": REASON_PROXIMITY, "detail": detail}
        if len(touched) == 2:
            return None
        asset = touched[0]
        b0 = touches[asset].floor("1min")
        if now >= b0 + pd.Timedelta(minutes=BLOCK_BARS):
            return None
        detail.update({"phase": 2, "touch_asset": asset,
                       "touch_time": touches[asset].isoformat(), "b0": b0.isoformat()})
        return {"reason": REASON_SWEEP, "detail": detail}
