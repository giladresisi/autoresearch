"""Audit + dual-event records (GIL-44 Phase 2, Wave 2.5).

Two writers:
  (a) events-native — AI decisions rendered into the SAME event shapes the hypothesis
      engine emits (a new-hypothesis-shaped dict + a daily-trend-shaped dict), marked
      "source":"ai-decisions" so existing analysis/plot/replay tooling reads them natively.
      Emitted into events.jsonl ONLY when the flag is ON (Phase 3).
  (b) AI-only audit JSONL (ai_decisions_audit.jsonl) — one self-contained record per call,
      designed from day one for the future evals/self-improvement loop. The `outcome`
      slot is null on write and filled by the Phase-4 annotation job.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The keys a real `new-hypothesis` event carries (confirmed from a live events_1s.jsonl,
# plan grounding). The AI events-native dict carries EVERY one of these plus `source`.
REAL_HYP_EVENT_KEYS = (
    "kind", "time", "direction", "price", "weekly_mid", "daily_mid", "last_liquidity",
    "targets", "cautious_price_initial", "cautious_price_initial_level",
    "cautious_price_secondary", "cautious_price_secondary_level", "entry_ranges",
    "direction_reason",
)

# next_move.direction ∈ {neutral,up,down} → hypothesis.direction ∈ {none,up,down}.
_NEXT_TO_HYP_DIR = {"neutral": "none", "up": "up", "down": "down"}


@dataclass
class DecisionRecord:
    """The engine's in-memory record for one AI decision (also serialised to the
    audit JSONL). `outcome` is filled later by the Phase-4 annotation job."""

    trigger_ts: Optional[str]
    arrival_ts: Optional[str]
    trigger_kind: str
    facts_content_hash: str
    decision: dict                       # {"daily_trend": ..., "next_move": ...}
    paired_diff: dict
    verdict: str                         # "clean" | "failsafe" | "guard-kill" | "churn-suppressed"
    fallback: bool
    error: Optional[str] = None
    standing_daily_trend: Optional[dict] = None
    audit: dict = field(default_factory=dict)
    outcome: Optional[dict] = None


def build_ai_hypothesis_event(next_block: dict, *, time_iso: Optional[str],
                              hyp_event: Optional[dict] = None,
                              now_price=None) -> dict:
    """The AI next-move rendered into a `new-hypothesis`-shaped event, source-marked.

    Decision-derived fields come from `next_block`; physical-context fields
    (price/mids/liquidity/entry_ranges) are carried from the paired real event when
    available (they are facts, not decisions)."""
    he = hyp_event or {}
    mt = next_block.get("move_target") or {}
    direction = _NEXT_TO_HYP_DIR.get(next_block.get("direction"), "none")
    targets = [mt] if mt.get("level") else []
    evt = {
        "kind": "new-hypothesis",
        "source": "ai-decisions",
        "time": time_iso,
        "direction": direction,
        "price": now_price if now_price is not None else he.get("price"),
        "weekly_mid": he.get("weekly_mid"),
        "daily_mid": he.get("daily_mid"),
        "last_liquidity": he.get("last_liquidity"),
        "targets": targets,
        "cautious_price_initial": mt.get("price"),
        "cautious_price_initial_level": mt.get("level"),
        "cautious_price_secondary": None,
        "cautious_price_secondary_level": next_block.get("flipped_target"),
        "entry_ranges": he.get("entry_ranges"),
        "direction_reason": next_block.get("flip_trigger"),
        # audit extras the real event lacks (harmless to consumers keyed on known fields)
        "arm_entry_confirmation": next_block.get("arm_entry_confirmation"),
        "confidence": next_block.get("confidence"),
    }
    return evt


def build_ai_daily_trend_event(daily_block: dict, *, time_iso: Optional[str]) -> dict:
    """The AI daily-trend rendered into a `daily-trend`-shaped event, source-marked."""
    return {
        "kind": "daily-trend",
        "source": "ai-decisions",
        "time": time_iso,
        "direction": daily_block.get("direction"),
        "confidence": daily_block.get("confidence"),
        "regime": daily_block.get("regime"),
        "S": daily_block.get("S"),
        "day_dol": daily_block.get("day_dol"),
        "weakens_to_neutral_if": daily_block.get("weakens_to_neutral_if"),
        "flips_if": daily_block.get("flips_if"),
    }


def build_paired_diff(hyp_event: Optional[dict], next_block: dict,
                      daily_block: dict) -> dict:
    """The hypothesis-vs-AI paired diff (the Field-mapping table): direction (none↔
    neutral), move target, entry-seeking, and daily-trend context."""
    he = hyp_event or {}
    hyp_dir = he.get("direction")
    hyp_dir_n = "neutral" if hyp_dir in (None, "none") else hyp_dir
    ai_dir = next_block.get("direction")
    ai_target = (next_block.get("move_target") or {}).get("level")
    hyp_target = he.get("cautious_price_initial_level")
    return {
        "direction": {"hypothesis": hyp_dir_n, "ai": ai_dir,
                      "agree": hyp_dir_n == ai_dir},
        "move_target": {"hypothesis": hyp_target, "ai": ai_target,
                        "agree": (hyp_target is not None and hyp_target == ai_target)},
        "entry_seeking": {"hypothesis_formed": hyp_dir_n != "neutral",
                          "ai_arm": next_block.get("arm_entry_confirmation"),
                          "ai_confidence": next_block.get("confidence")},
        "daily_trend_context": {"ai_direction": daily_block.get("direction"),
                                "ai_confidence": daily_block.get("confidence"),
                                "ai_regime": daily_block.get("regime")},
    }


def snapshot_ref(snapshot_text: str, content_hash: str, snapshots_dir,
                 inline_max: int) -> dict:
    """Small snapshot → inline; large snapshot → stored by reference (file+hash)."""
    if len(snapshot_text) <= inline_max:
        return {"inline": snapshot_text, "hash": content_hash}
    snapshots_dir = Path(snapshots_dir)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    path = snapshots_dir / f"{content_hash}.txt"
    if not path.exists():
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(snapshot_text)
    return {"ref": str(path), "hash": content_hash}


def resolve_snapshot(ref: dict) -> str:
    """Re-resolve a snapshot_ref (inline or file) back to its text."""
    if "inline" in ref:
        return ref["inline"]
    with open(ref["ref"], encoding="utf-8", newline="") as fh:
        return fh.read()


def write_audit_record(path, record: dict) -> None:
    """Append one audit record as a JSONL line (atomic-ish append)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def read_audit_records(path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
