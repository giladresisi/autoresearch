"""The fact record. One shape, per-class extras in `extra`.

State is an enum plus a timestamp, never a pile of booleans — booleans proliferate
and drift out of consistency. `price` (wick extreme) and the body/close extreme in
`extra["body_price"]` are BOTH carried: thesis.md's P1/P2 criteria turn on the
distinction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class FactClass(str, Enum):
    LEVEL = "level"
    FVG = "fvg"
    LEG = "leg"
    EXTREME = "extreme"
    ANCHOR = "anchor"


class FactState(str, Enum):
    LIVE = "live"
    SWEPT = "swept"
    DEPLETED = "depleted"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"
    TRAVERSED_BY_GAP = "traversed_by_gap"


@dataclass
class Fact:
    id: str
    cls: FactClass
    ticker: str
    label: str
    name: "str | None"                 # mutable, current (e.g. prev2_day_high); levels only
    reference_ts: pd.Timestamp
    price: "float | None"
    price_low: "float | None"
    price_high: "float | None"
    timeframe: "str | None"
    resolution: str
    state: FactState
    state_ts: pd.Timestamp
    provenance: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def set_state(self, state: FactState, ts: pd.Timestamp) -> None:
        self.state = state
        self.state_ts = ts

    # -- serialization (journal / snapshot) ---------------------------------- #

    def to_dict(self) -> dict:
        """JSON-safe view. Timestamps become ISO strings; enums become their values."""
        return {
            "id": self.id,
            "cls": self.cls.value,
            "ticker": self.ticker,
            "label": self.label,
            "name": self.name,
            "reference_ts": _iso(self.reference_ts),
            "price": self.price,
            "price_low": self.price_low,
            "price_high": self.price_high,
            "timeframe": self.timeframe,
            "resolution": self.resolution,
            "state": self.state.value,
            "state_ts": _iso(self.state_ts),
            "provenance": _jsonable(self.provenance),
            "extra": _jsonable(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Fact":
        return cls(
            id=d["id"],
            cls=FactClass(d["cls"]),
            ticker=d["ticker"],
            label=d.get("label", ""),
            name=d.get("name"),
            reference_ts=_ts(d.get("reference_ts")),
            price=d.get("price"),
            price_low=d.get("price_low"),
            price_high=d.get("price_high"),
            timeframe=d.get("timeframe"),
            resolution=d.get("resolution", "1min"),
            state=FactState(d.get("state", "live")),
            state_ts=_ts(d.get("state_ts")),
            provenance=dict(d.get("provenance") or {}),
            extra=dict(d.get("extra") or {}),
        )


def _iso(ts) -> "str | None":
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _ts(v):
    if v is None:
        return None
    return v if isinstance(v, pd.Timestamp) else pd.Timestamp(v)


def _jsonable(obj):
    """Best-effort JSON coercion for `extra`/`provenance` (timestamps, sets, numpy)."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_jsonable(v) for v in obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            return obj.item()
        except Exception:
            return str(obj)
    return obj
