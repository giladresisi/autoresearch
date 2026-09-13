"""Code-owned confidence module (spec §8) — the L1→L2 gate.

The model's self-reported confidence is AUDIT-ONLY (batch-proven inverted: self-reported
`high` was 0% correct, `low` 41%). Effective confidence is computed here in code:

  features (deterministic, from the decision + facts)  →  calibration-table cell  →
  historical hit-rate  →  gate thresholds  →  HIGH / MEDIUM / LOW.

The table is a loadable artifact (JSON) bootstrapped from the audit archive (the 289-trigger
batch); it is recalibrated as primary-mode audits accumulate. Mapping VALUES are
placeholder-quality until the internals effort lands — that is expected and documented; the
STRUCTURE (the feature set, the cell key, the data flow, the gate) is what this reserves.

Public gate (the P2 contract): `confidence(decision, facts) -> "HIGH"|"MEDIUM"|"LOW"`. A
missing table cell returns the configured floor (LOW) so an unseen situation never
over-claims confidence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TABLE_PATH = os.path.join(_HERE, "calibration_table.json")

TIERS = ("HIGH", "MEDIUM", "LOW")


# --------------------------------------------------------------------------- #
# Feature extraction                                                          #
# --------------------------------------------------------------------------- #
def _num_list(items, key):
    return [i.get(key) for i in items
            if isinstance(i, dict) and isinstance(i.get(key), (int, float))]


def _ledger_items(decision: dict) -> list:
    """The bull+bear ledger items of a v1 next_move block (the batch's evidence shape).
    A v2 thesis carries no ledger yet (internals deferred) → empty."""
    nm = decision.get("next_move") if "next_move" in decision else decision
    if not isinstance(nm, dict):
        return []
    return list(nm.get("bull_ledger") or []) + list(nm.get("bear_ledger") or [])


def _score_abs(decision: dict) -> float:
    nm = decision.get("next_move") if "next_move" in decision else decision
    for key in ("N", "S"):
        v = (nm or {}).get(key) if isinstance(nm, dict) else None
        if isinstance(v, (int, float)):
            return abs(float(v))
    return 0.0


def _regime(decision: dict, facts: Optional[dict]) -> str:
    dt = decision.get("daily_trend") if isinstance(decision, dict) else None
    for src in (dt, decision, facts or {}):
        if isinstance(src, dict) and src.get("regime"):
            return str(src["regime"]).upper()
    return "UNKNOWN"


def extract_features(decision: dict, facts: Optional[dict] = None) -> dict:
    """Deterministic features from the decision + facts (spec §8). Works on the v1
    next_move ledger shape (the calibration substrate); a ledger-less v2 thesis yields
    zero-evidence features → the floor tier."""
    decision = decision or {}
    items = _ledger_items(decision)
    types = [i.get("type") for i in items if isinstance(i, dict) and i.get("type")]
    tiers = _num_list(items, "tier")
    fresh = _num_list(items, "freshness")
    return {
        "evidence_count": len(items),
        "item_type_diversity": len(set(types)),
        "tier_max": max(tiers) if tiers else 0.0,
        "freshness_mean": round(sum(fresh) / len(fresh), 4) if fresh else 0.0,
        "freshness_min": min(fresh) if fresh else 0.0,
        "score_abs": _score_abs(decision),
        "regime": _regime(decision, facts),
        "session_phase": str((facts or {}).get("session_phase") or "unknown"),
    }


def _score_band(score_abs: float) -> str:
    if score_abs >= 6:
        return "6+"
    if score_abs >= 3:
        return "3-6"
    return "0-3"


def _evidence_band(n: int) -> str:
    if n >= 6:
        return "6+"
    if n >= 3:
        return "3-5"
    if n >= 1:
        return "1-2"
    return "0"


def cell_key(features: dict) -> str:
    """The calibration-table lookup key. Deliberately coarse (regime × score-band ×
    evidence-band) so the ~289-trigger bootstrap keeps cells populated; the finer features
    (freshness/tier/diversity/session_phase) are extracted and stored for recalibration but
    kept out of the key to avoid a sparse table (documented placeholder decision)."""
    return f"{features.get('regime', 'UNKNOWN')}|{_score_band(features.get('score_abs', 0.0))}" \
           f"|{_evidence_band(features.get('evidence_count', 0))}"


# --------------------------------------------------------------------------- #
# Calibration table + gate                                                    #
# --------------------------------------------------------------------------- #
@dataclass
class CalibrationTable:
    cells: dict = field(default_factory=dict)          # cell_key -> {"hit_rate", "n"}
    thresholds: dict = field(default_factory=lambda: {"high": 0.60, "medium": 0.45})
    floor: str = "LOW"
    min_samples: int = 5                               # cells below this fall to the floor

    def lookup(self, key: str):
        cell = self.cells.get(key)
        if not cell or cell.get("n", 0) < self.min_samples:
            return None
        return cell.get("hit_rate")

    def gate(self, hit_rate) -> str:
        if hit_rate is None:
            return self.floor
        if hit_rate >= self.thresholds.get("high", 0.60):
            return "HIGH"
        if hit_rate >= self.thresholds.get("medium", 0.45):
            return "MEDIUM"
        return "LOW"

    def to_dict(self) -> dict:
        return {"thresholds": self.thresholds, "floor": self.floor,
                "min_samples": self.min_samples, "cells": self.cells}

    @classmethod
    def from_dict(cls, d: dict) -> "CalibrationTable":
        d = d or {}
        return cls(cells=d.get("cells", {}),
                   thresholds=d.get("thresholds", {"high": 0.60, "medium": 0.45}),
                   floor=d.get("floor", "LOW"), min_samples=d.get("min_samples", 5))

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path=DEFAULT_TABLE_PATH) -> "CalibrationTable":
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except Exception:
            return cls()                               # empty → everything hits the floor


_DEFAULT_TABLE: Optional[CalibrationTable] = None


def _default_table() -> CalibrationTable:
    global _DEFAULT_TABLE
    if _DEFAULT_TABLE is None:
        _DEFAULT_TABLE = CalibrationTable.load()
    return _DEFAULT_TABLE


def confidence(decision, facts: Optional[dict] = None,
               table: Optional[CalibrationTable] = None) -> str:
    """The L1→L2 gate (spec §8): decision → features → cell → hit-rate → tier. A missing/
    under-sampled cell returns the floor (LOW). Accepts a Thesis/dataclass (via .to_dict),
    a raw dict, or a full {daily_trend, next_move} decision."""
    if hasattr(decision, "to_dict"):
        decision = decision.to_dict()
    tbl = table or _default_table()
    feats = extract_features(decision or {}, facts)
    return tbl.gate(tbl.lookup(cell_key(feats)))


# --------------------------------------------------------------------------- #
# Bootstrap table build (from the batch audit archive)                        #
# --------------------------------------------------------------------------- #
def _session_phase_from_ts(ts) -> str:
    try:
        import pandas as pd
        t = pd.Timestamp(ts)
        m = t.hour * 60 + t.minute
    except Exception:
        return "unknown"
    if 18 * 60 <= m or m < 2 * 60:
        return "asia"
    if 2 * 60 <= m < 9 * 60 + 20:
        return "london"
    if 9 * 60 + 20 <= m < 12 * 60:
        return "ny_am"
    return "ny_pm"


def build_bootstrap_table(audit_paths, table: Optional[CalibrationTable] = None
                          ) -> CalibrationTable:
    """Accumulate per-cell hit-rate from the batch audit JSONL files: for every record with
    a next_move ledger and an `outcome.ai_correct`, extract features and tally correctness
    into its cell. Values are placeholder-quality (small n per cell) — that is acceptable and
    documented; the point is the data flow, not the numbers."""
    tbl = table or CalibrationTable()
    tally: dict = {}
    for path in audit_paths:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            outcome = rec.get("outcome") or {}
            correct = outcome.get("ai_correct")
            if not isinstance(correct, bool):
                continue
            decision = rec.get("decision") or {}
            facts = {"session_phase": _session_phase_from_ts(rec.get("trigger_ts"))}
            key = cell_key(extract_features(decision, facts))
            t = tally.setdefault(key, [0, 0])          # [hits, n]
            t[0] += 1 if correct else 0
            t[1] += 1
    tbl.cells = {k: {"hit_rate": round(h / n, 4), "n": n}
                 for k, (h, n) in tally.items() if n > 0}
    return tbl


def _main(argv=None) -> int:
    import argparse
    import glob
    ap = argparse.ArgumentParser(description="Build the confidence calibration bootstrap table")
    ap.add_argument("--audit-glob",
                    default=os.path.join(os.path.dirname(_HERE),
                                         "regression", "sessions", "*", "*",
                                         "ai_decisions_audit.jsonl"),
                    help="glob for batch ai_decisions_audit.jsonl files")
    ap.add_argument("--out", default=DEFAULT_TABLE_PATH)
    args = ap.parse_args(argv)
    paths = sorted(glob.glob(args.audit_glob))
    tbl = build_bootstrap_table(paths)
    tbl.save(args.out)
    total = sum(c["n"] for c in tbl.cells.values())
    print(f"built {len(tbl.cells)} cells from {len(paths)} audit files "
          f"({total} labelled triggers) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
