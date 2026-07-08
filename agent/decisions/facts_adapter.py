"""Online facts adapter (GIL-44 Phase 2, Wave 2.1).

Feeds the Phase-1 derive_facts pure functions from the pipeline's LIVE rolling frames
(no parquet re-reads — decision D1: recompute-from-frames), producing the SAME S0–S7 +
S3b fact sheet the offline bench was validated on. This is the whole point of D1: reuse
the validated schema so the online semantic layer and the consistency check are trivially
aligned.

build_snapshot returns a Snapshot{text, validator_dict, content_hash, max_ts, ...}:
  - text            — the rendered fact sheet (identical bytes to the offline slice path)
  - validator_dict  — the JSON view validator._check_semantic consumes
  - content_hash    — sha256 of the canonical validator_dict → churn-guard key + drift
                      detector (same trigger + same facts always hashes identically)
  - max_ts          — the true max bar timestamp that fed the snapshot (no-lookahead guard)

Live frames carry capitalised OHLC(V) columns; this adapter reproduces derive_facts.load
normalisation (lowercase o/h/l/c, tz-aware ET, CME-maintenance bars dropped) before
calling compute_facts.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from derive_facts import (  # noqa: E402
    TZ,
    compute_facts,
    facts_to_validator_dict,
    render_facts_text,
    session_frame,
    trade_date,
)

_MAINT_LO = datetime.time(16, 55)
_MAINT_HI = datetime.time(18, 0)


@dataclass
class Snapshot:
    text: str
    validator_dict: dict
    content_hash: str
    max_ts: Optional[pd.Timestamp]
    now: Optional[pd.Timestamp] = None
    checkpoint: Optional[pd.Timestamp] = None
    degraded: bool = False
    error: Optional[str] = None


def _normalize(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Reproduce derive_facts.load() normalisation on an in-memory live frame:
    lowercase o/h/l/c, tz-aware ET index, CME-maintenance bars dropped."""
    if df is None:
        return None
    d = df.rename(columns=str.lower)
    d = d[["open", "high", "low", "close"]]
    if d.index.tz is None:
        d.index = d.index.tz_localize(TZ)
    t = d.index.time
    keep = (t <= _MAINT_LO) | (t >= _MAINT_HI)
    return d[keep]


def _has_session(df: Optional[pd.DataFrame], now: pd.Timestamp) -> bool:
    if df is None or len(df) == 0:
        return False
    return len(session_frame(df, trade_date(now))) > 0


def _frame_max_ts(frames) -> Optional[pd.Timestamp]:
    mx = None
    for f in frames:
        if f is not None and len(f):
            last = f.index[-1]
            mx = last if mx is None else max(mx, last)
    return mx


def _canonical_hash(validator_dict: dict) -> str:
    payload = json.dumps(validator_dict, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_EMPTY_HASH = hashlib.sha256(b"<no-data>").hexdigest()


def build_snapshot(frames: dict, *, checkpoint: Optional[pd.Timestamp] = None) -> Snapshot:
    """Build a fact snapshot from the live frames.

    `frames` := {mnq_today, mes_today, hist_mnq, hist_mes, hist_1hr, hist_4hr, ath_mnq,
    ath_mes, now}. For a daily-trend checkpoint pass `checkpoint=<ET timestamp>` — the
    facts are truncated at the checkpoint even when invoked later. For a next-move
    trigger leave it None → truncation at frames["now"] (the trigger timestamp).
    """
    now = checkpoint if checkpoint is not None else frames["now"]

    mnq = _normalize(frames.get("mnq_today"))
    mes = _normalize(frames.get("mes_today"))
    hist_mnq = _normalize(frames.get("hist_mnq"))
    hist_mes = _normalize(frames.get("hist_mes"))

    mnq_t = mnq[mnq.index <= now] if mnq is not None else None
    mes_t = mes[mes.index <= now] if mes is not None else None

    # Degraded: either ticker lacks bars in the current session (e.g. empty MES at the
    # 18:00 open, backtest_smt.py:1336). Return a marked snapshot; the engine records a
    # failsafe and makes no API call — it never crashes the pipeline.
    if not _has_session(mnq_t, now) or not _has_session(mes_t, now):
        return Snapshot(text="", validator_dict={}, content_hash=_EMPTY_HASH,
                        max_ts=now, now=now, checkpoint=checkpoint, degraded=True,
                        error="insufficient-data")

    bundle = compute_facts(
        mnq_t, mes_t,
        ath_mnq=frames.get("ath_mnq"), ath_mes=frames.get("ath_mes"),
        hist_mnq=hist_mnq, hist_mes=hist_mes, now=now,
    )
    text = render_facts_text(bundle)
    vdict = facts_to_validator_dict(bundle)
    return Snapshot(
        text=text, validator_dict=vdict, content_hash=_canonical_hash(vdict),
        max_ts=_frame_max_ts([mnq_t, mes_t]), now=now, checkpoint=checkpoint,
    )
