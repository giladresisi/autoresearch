"""Liquidity-exhaustion flip (session 2026-10-15 O2) -- `l2-mechanisms.md` §11.13, a
CANDIDATE, ON by default (`ACT_LIQ_EXHAUST_FLIP`; operator decision 2026-10-19).

Operator, 2026-10-15: after a ~1900-pt multi-day drop, MNQ reached the untouched Sep 17
daily bull FVG (09:54), stalled, and the first 1m FVG formed in BOTH assets after the stall
(the 10:00 bar) was the long of the day: 10:01:00 @ 29708.25, MFE 120.75 against a 34.5-pt
stop. The DOWN plan was already dead (positive_close 09:36), and no mechanism enters against
the thesis after the arm.

Readings confirmed by the operator 2026-10-18:

  GATE      the 09:20 HTF context (`htf_liquidity`, PR #137): MNQ's move in the PLAN's
            direction is >= `MIN_SIZE_ADR` x ADR and still "ongoing" (no 25% retrace).
  LEVELS    that context's levels AHEAD of price in the plan direction, on EITHER asset,
            status "untouched" at 09:19, tiers `TIERS` (4h / daily / weekly FVGs, prior-day
            and prior-week extremes; 6h-session extremes excluded -- the worst group in the
            2026-10-16 research). A level touched before `WATCH_FROM` is consumed.
  TOUCH     a completed 1m bar of the level's asset trades strictly beyond the level's near
            edge (into an FVG zone, through an extreme), its bar labelled in
            [`WATCH_FROM`, `WATCH_UNTIL`).
  VOID      an FVG level whose far edge is traded through is dropped (the gap filled).
  STALL     on a completed 1m bar at/after the touch, before `WATCH_UNTIL`: (a) the last
            two completed 1m bars closed against the plan in BOTH assets, or (b) the level's
            asset closed back out of the level (above a low-side level for a DOWN plan).
  FLIP      at the stall, when flat: the standing plan is replaced (the operator direction
            path) by one in the opposite direction that arms ONLY `fvg_1m_both`, budget one
            attempt. Once a day; never back. A position open at the stall SKIPS the flip and
            ends the watch for the day.
  ENTRY     `fvg_1m_both`: the first 1m FVG in the new direction formed in BOTH assets and
            completed after the flip; market at its third bar's close; stop the MNQ gap's
            far edge one tick out; every market-mechanism veto applies; none at/after 10:30.

This module owns no wiring: the graft drives `LiqWatch` and replaces the plan, the Executor
asks `both_fvg_fire` on the flipped plan. Pure: no clock, no I/O.
"""
from __future__ import annotations

import os

import pandas as pd

ENV_FLAG = "ACT_LIQ_EXHAUST_FLIP"
#: Harness override: None reads the env var; True / False force it.
ENABLED = None
#: Unset or empty env var = this (operator decision 2026-10-19).
DEFAULT_ENABLED = True

MECHANISM = "fvg_1m_both"
MIN_SIZE_ADR = 3.0
TIERS = ("fvg_4h", "fvg_daily", "fvg_weekly", "day", "week")
WATCH_FROM = (9, 35)
WATCH_UNTIL = (10, 30)
CONTEXT_FROM = (9, 20)
TICK = 0.25
ASSETS = ("MNQ", "MES")
_SHORT = ("DOWN", "SHORT")
_ONE = pd.Timedelta(minutes=1)


def enabled() -> bool:
    """Read ONCE per graft, at its construction -- never inside the bar loop. Unset or
    empty = `DEFAULT_ENABLED`; `0`/`false`/`no`/`off` = off; `1`/`true`/`yes`/`on` = on."""
    if ENABLED is not None:
        return bool(ENABLED)
    raw = str(os.environ.get(ENV_FLAG, "")).strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return DEFAULT_ENABLED


def is_short(direction) -> bool:
    return str(direction or "").upper() in _SHORT


def opposite(direction) -> str:
    return "UP" if is_short(direction) else "DOWN"


def _at(now: pd.Timestamp, hm) -> pd.Timestamp:
    return now.normalize() + pd.Timedelta(hours=hm[0], minutes=hm[1])


def bar_1m(frame, label: pd.Timestamp) -> "dict | None":
    """The 1m OHLC of the minute labelled `label` (left-labelled) from a 1s or 1m frame."""
    if frame is None or not len(frame):
        return None
    idx = frame.index
    i, j = idx.searchsorted(label, side="left"), idx.searchsorted(label + _ONE, side="left")
    if j <= i:
        return None
    seg = frame.iloc[i:j]
    return {"Open": float(seg["Open"].iloc[0]), "High": float(seg["High"].max()),
            "Low": float(seg["Low"].min()), "Close": float(seg["Close"].iloc[-1])}


def watch_levels(ctx: "dict | None", direction) -> "tuple[list, dict]":
    """(levels, gate) from the 09:20 HTF context for a plan in `direction`. `levels` is
    empty when the gate fails; `gate` says why, for the record."""
    if not isinstance(ctx, dict) or ctx.get("status") not in ("OK", "PARTIAL"):
        return [], {"ok": False, "reason": "no_context"}
    mnq = (ctx.get("assets") or {}).get("MNQ") or {}
    want = "DOWN" if is_short(direction) else "UP"
    move = (mnq.get("moves") or {}).get(want) or {}
    size_adr = move.get("size_adr")
    gate = {"direction": want, "size_adr": size_adr, "state": move.get("state"),
            "size": move.get("size"), "origin": move.get("origin"),
            "extreme": move.get("extreme")}
    if size_adr is None or float(size_adr) < MIN_SIZE_ADR:
        return [], {**gate, "ok": False, "reason": "move_too_small"}
    if move.get("state") != "ongoing":
        return [], {**gate, "ok": False, "reason": "move_retracing"}
    side = "below" if is_short(direction) else "above"
    out = []
    for tk in ASSETS:
        for lv in ((ctx.get("assets") or {}).get(tk) or {}).get("levels") or ():
            if (lv.get("tier") not in TIERS or lv.get("side") != side
                    or lv.get("status") != "untouched"):
                continue
            zone = lv.get("zone")
            if zone:
                lo, hi = float(min(zone)), float(max(zone))
                near, far = (hi, lo) if side == "below" else (lo, hi)
            else:
                near, far = float(lv["price"]), None
            out.append({"asset": tk, "tier": lv["tier"], "name": lv.get("name"),
                        "near": near, "far": far})
    return out, {**gate, "ok": bool(out), "reason": "ok" if out else "no_level_ahead",
                 "n_levels": len(out)}


class LiqWatch:
    """The per-day watch. `on_bar_close` returns a stall event once, then nothing."""

    def __init__(self, direction, levels: list, gate: dict) -> None:
        self.direction = str(direction or "").upper()
        self._short = is_short(direction)
        self.gate = dict(gate or {})
        self.levels = [dict(lv, touched=None, voided=None) for lv in levels or ()]
        self.done = None if self.levels else "no_levels"
        self._last_label = None

    def state(self) -> dict:
        return {"direction": self.direction, "done": self.done, "gate": self.gate,
                "levels": [{k: (str(v) if isinstance(v, pd.Timestamp) else v)
                            for k, v in lv.items()} for lv in self.levels]}

    def finish(self, reason: str) -> None:
        self.done = reason

    def _beyond_near(self, lv, bar) -> bool:
        return bar["Low"] < lv["near"] if self._short else bar["High"] > lv["near"]

    def _beyond_far(self, lv, bar) -> bool:
        if lv["far"] is None:
            return False
        return bar["Low"] < lv["far"] if self._short else bar["High"] > lv["far"]

    def _against(self, bar) -> bool:
        """Closed against the plan (up for a DOWN plan)."""
        return bar["Close"] > bar["Open"] if self._short else bar["Close"] < bar["Open"]

    def _back_out(self, lv, bar) -> bool:
        return bar["Close"] > lv["near"] if self._short else bar["Close"] < lv["near"]

    def on_bar_close(self, now: pd.Timestamp, frames: dict) -> "dict | None":
        if self.done is not None or now is None:
            return None
        label = now.floor("1min") - _ONE
        start = _at(now, CONTEXT_FROM)
        # Every completed minute since the context boundary is judged exactly once, so a
        # watch built late (the thesis call is threaded) still sees the 09:20+ bars.
        first = start if self._last_label is None else self._last_label + _ONE
        event = None
        t = first
        while t <= label and event is None:
            event = self._judge(t, now, frames)
            t += _ONE
        self._last_label = label
        return event

    def _judge(self, label, now, frames) -> "dict | None":
        w_from, w_until = _at(now, WATCH_FROM), _at(now, WATCH_UNTIL)
        bars = {tk: bar_1m((frames or {}).get(tk), label) for tk in ASSETS}
        prev = {tk: bar_1m((frames or {}).get(tk), label - _ONE) for tk in ASSETS}
        if label >= w_until:
            self.done = "window_end"
            return None
        for lv in self.levels:
            bar = bars.get(lv["asset"])
            if lv["voided"] is not None or bar is None:
                continue
            if lv["touched"] is None and self._beyond_near(lv, bar):
                if label < w_from:
                    lv["voided"] = "touched_before_window"
                    continue
                lv["touched"] = label
            if lv["touched"] is not None and self._beyond_far(lv, bar):
                lv["voided"] = "far_edge"
        for lv in self.levels:
            if lv["touched"] is None or lv["voided"] is not None:
                continue
            bar = bars.get(lv["asset"])
            kind = None
            if bar is not None and self._back_out(lv, bar):
                kind = "close_back_out"
            elif (label - _ONE >= lv["touched"]
                  and all(bars.get(tk) is not None and prev.get(tk) is not None
                          and self._against(bars[tk]) and self._against(prev[tk])
                          for tk in ASSETS)):
                kind = "two_closes_both"
            if kind is not None:
                self.done = "stalled"
                return {"level": {k: (str(v) if isinstance(v, pd.Timestamp) else v)
                                  for k, v in lv.items()},
                        "stall": kind, "bar": label.isoformat(),
                        "to": opposite(self.direction)}
        return None


def both_fvg_fire(now: pd.Timestamp, mnq, mes, direction, since: pd.Timestamp) -> "dict | None":
    """`fvg_1m_both`: a 1m FVG in `direction` in BOTH assets whose third bar just completed
    at `now`, at or after `since` (the flip). Market at that bar's close."""
    if now is None:
        return None
    b3_label = now.floor("1min") - _ONE
    if since is not None and b3_label < since.floor("1min"):
        return None
    b1_label = b3_label - 2 * _ONE
    short = is_short(direction)
    gaps = {}
    for tk, frame in (("MNQ", mnq), ("MES", mes)):
        b1, b3 = bar_1m(frame, b1_label), bar_1m(frame, b3_label)
        if b1 is None or b3 is None:
            return None
        if short and b3["High"] < b1["Low"]:
            gaps[tk] = (b3["High"], b1["Low"])
        elif not short and b3["Low"] > b1["High"]:
            gaps[tk] = (b1["High"], b3["Low"])
        else:
            return None
        if tk == "MNQ":
            close = b3["Close"]
            stop = b1["Low"] + TICK if short else b1["High"] - TICK
    return {"time": now, "price": close, "stop": stop,
            "direction": "DOWN" if short else "UP", "mechanism": MECHANISM,
            "gap_id": f"{MECHANISM} {b3_label.isoformat()}",
            "gaps": {k: [v[0], v[1]] for k, v in gaps.items()},
            "bar": b3_label.isoformat()}
