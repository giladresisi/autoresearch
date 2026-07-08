"""End-of-session outcome annotation (GIL-44 Phase 4, Wave 4.1).

Fills each audit record's `outcome` slot from bars AFTER `arrival_ts` over the finished
session parquet. Truth labels REUSE the calibration constants (score_results.py
MOVE_MIN / RATIO / DAY_MIN) so shadow outcomes are directly comparable to the Phase-4
calibration ground truth (decision D5).

Outcome captures: the realized direction at the 4h horizon (calibration label), whether
the move_target was reached before a flip against the AI's direction, MFE/MAE, and the
hypothesis-vs-AI-vs-price correctness verdicts.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_CALIB = os.path.join(os.path.dirname(_AGENT), "calibration")
for _p in (_HERE, _AGENT, _CALIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from records import read_audit_records, write_audit_record  # noqa: E402

# Calibration truth-label constants (score_results.py) — the single source (D5).
try:
    from score_results import DAY_MIN, MOVE_MIN, RATIO  # noqa: E402
except Exception:                                        # pragma: no cover
    MOVE_MIN, RATIO, DAY_MIN = 100.0, 2.0, 150.0

HORIZON = pd.Timedelta(hours=4)


def _align_tz(ts: pd.Timestamp, tz):
    """Normalise a parsed timestamp to `tz` so a fixed-offset ISO string ('...-04:00')
    compares cleanly against a pytz-zoned bar index ('America/New_York')."""
    if tz is None:
        return ts
    return ts.tz_localize(tz) if ts.tzinfo is None else ts.tz_convert(tz)


def _label_next(up_exc: float, dn_exc: float) -> str:
    if up_exc >= MOVE_MIN and up_exc >= RATIO * dn_exc:
        return "up"
    if dn_exc >= MOVE_MIN and dn_exc >= RATIO * up_exc:
        return "down"
    return "chop"


def _dir_correct(decision_dir: Optional[str], realized: str) -> Optional[bool]:
    if decision_dir is None:
        return None
    return ((decision_dir == "up" and realized == "up")
            or (decision_dir == "down" and realized == "down")
            or (decision_dir in ("neutral", "none") and realized == "chop"))


def _target_vs_flip(post: pd.DataFrame, direction: str, ref: float,
                    target_price: Optional[float]) -> str:
    """Walk forward: which came first — reaching the move_target, or a flip (price moving
    MOVE_MIN against the AI direction from the reference)? Returns target/flip/neither."""
    if direction not in ("up", "down"):
        return "neither"
    for _, row in post.iterrows():
        hi, lo = float(row["High"]), float(row["Low"])
        if direction == "up":
            if target_price is not None and hi >= target_price:
                return "target"
            if ref - lo >= MOVE_MIN:
                return "flip"
        else:
            if target_price is not None and lo <= target_price:
                return "target"
            if hi - ref >= MOVE_MIN:
                return "flip"
    return "neither"


def annotate_record(record: dict, bars: pd.DataFrame, horizon: pd.Timedelta = HORIZON) -> dict:
    """Compute the outcome dict for one audit record from post-arrival bars."""
    arrival = _align_tz(pd.Timestamp(record.get("arrival_ts")), bars.index.tz)
    post = bars[bars.index >= arrival]
    if len(post) == 0:
        return {"status": "truncated"}

    at_or_before = bars[bars.index <= arrival]
    ref = float(at_or_before["Close"].iloc[-1]) if len(at_or_before) else float(post["Open"].iloc[0])
    post_h = post[post.index <= arrival + horizon]
    if len(post_h) == 0:
        post_h = post.iloc[:1]

    up_exc = max(0.0, float(post_h["High"].max()) - ref)
    dn_exc = max(0.0, ref - float(post_h["Low"].min()))
    realized = _label_next(up_exc, dn_exc)

    nxt = (record.get("decision") or {}).get("next_move") or {}
    ai_dir = nxt.get("direction")
    mt = nxt.get("move_target") or {}
    target_price = mt.get("price")
    target_outcome = _target_vs_flip(post_h, ai_dir, ref, target_price)

    if ai_dir == "up":
        mfe, mae = up_exc, dn_exc
    elif ai_dir == "down":
        mfe, mae = dn_exc, up_exc
    else:
        mfe, mae = max(up_exc, dn_exc), min(up_exc, dn_exc)

    hyp_dir = ((record.get("paired_diff") or {}).get("direction") or {}).get("hypothesis")
    return {
        "status": "ok",
        "ref_price": round(ref, 2),
        "realized_direction": realized,
        "up_exc": round(up_exc, 2),
        "dn_exc": round(dn_exc, 2),
        "target_outcome": target_outcome,
        "mfe": round(mfe, 2),
        "mae": round(mae, 2),
        "ai_direction": ai_dir,
        "ai_correct": _dir_correct(ai_dir, realized),
        "hypothesis_direction": hyp_dir,
        "hypothesis_correct": _dir_correct(hyp_dir, realized),
    }


def _as_bars(session_parquet) -> pd.DataFrame:
    if isinstance(session_parquet, pd.DataFrame):
        bars = session_parquet
    else:
        bars = pd.read_parquet(session_parquet)
    if bars.index.tz is None:
        bars.index = bars.index.tz_localize("America/New_York")
    return bars


def annotate_session(audit_path, session_parquet, horizon: pd.Timedelta = HORIZON) -> list:
    """Annotate every record in the audit JSONL in place and rewrite the file."""
    records = read_audit_records(audit_path)
    if not records:
        return []
    bars = _as_bars(session_parquet)
    for rec in records:
        rec["outcome"] = annotate_record(rec, bars, horizon)
    # Rewrite the file atomically-enough: truncate + re-append.
    open(audit_path, "w", encoding="utf-8").close()
    for rec in records:
        write_audit_record(audit_path, rec)
    return records
