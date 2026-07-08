"""Comparison harness (GIL-44 Phase 4, Wave 4.2).

Produces the side-by-side table — per trigger, the hypothesis output vs the AI decision
as of arrival — plus per-day cost/latency stats and paired-diff agree/disagree counts by
field. Consumes shadow_audit.jsonl (and, optionally, the baseline events.jsonl for the
hypothesis side, though the paired_diff already carries it).
"""

from __future__ import annotations

import os
import sys
from statistics import mean

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_HERE, _AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from records import read_audit_records  # noqa: E402

# Rough claude-haiku-4.5 rates ($/1M tokens) for a cost ESTIMATE only.
_RATE_IN, _RATE_OUT, _RATE_CACHE = 1.0, 5.0, 0.10


def _est_cost(inp: float, out: float, cache_read: float) -> float:
    return round((inp * _RATE_IN + out * _RATE_OUT + cache_read * _RATE_CACHE) / 1e6, 4)


def build_comparison(audit_path, baseline_events_path=None) -> dict:
    records = read_audit_records(audit_path)
    rows = []
    agree = {"direction": 0, "move_target": 0}
    disagree = {"direction": 0, "move_target": 0}
    latencies = []
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    api_calls = cache_hits = 0
    n_triggers = n_checkpoints = 0

    for r in records:
        # Checkpoint records carry no paired_diff (daily-trend only) — they are NOT
        # hypothesis triggers and must not skew the agree/disagree counts.
        is_trigger = r.get("paired_diff") is not None
        pd_ = r.get("paired_diff") or {}
        dirb = pd_.get("direction") or {}
        tgtb = pd_.get("move_target") or {}
        entry = pd_.get("entry_seeking") or {}
        oc = r.get("outcome") or {}
        rows.append({
            "trigger_ts": r.get("trigger_ts"),
            "arrival_ts": r.get("arrival_ts"),
            "trigger_kind": r.get("trigger_kind"),
            "hyp_direction": dirb.get("hypothesis"),
            "ai_direction": dirb.get("ai"),
            "direction_agree": dirb.get("agree"),
            "hyp_target": tgtb.get("hypothesis"),
            "ai_target": tgtb.get("ai"),
            "ai_confidence": entry.get("ai_confidence"),
            "verdict": r.get("verdict"),
            "latency_sec": r.get("latency_total_sec"),
            "cached": r.get("cached"),
            "realized": oc.get("realized_direction"),
            "ai_correct": oc.get("ai_correct"),
            "hyp_correct": oc.get("hypothesis_correct"),
        })
        if is_trigger:
            n_triggers += 1
            (agree if dirb.get("agree") else disagree)["direction"] += 1
            (agree if tgtb.get("agree") else disagree)["move_target"] += 1
        else:
            n_checkpoints += 1
        lat = r.get("latency_total_sec")
        if lat:
            latencies.append(lat)
        u = r.get("usage_total") or {}
        for k in usage:
            usage[k] += u.get(k, 0) or 0
        if r.get("cached"):
            cache_hits += 1
        else:
            api_calls += 1

    # Aggregate AI-vs-hypothesis correctness where outcomes are annotated.
    ai_hits = sum(1 for row in rows if row["ai_correct"] is True)
    hyp_hits = sum(1 for row in rows if row["hyp_correct"] is True)
    scored = sum(1 for row in rows if row["ai_correct"] is not None)

    summary = {
        "n_records": len(records),
        "n_triggers": n_triggers,
        "n_checkpoints": n_checkpoints,
        "agree": agree,
        "disagree": disagree,
        "mean_latency_sec": round(mean(latencies), 1) if latencies else None,
        "usage": usage,
        "api_calls": api_calls,
        "cache_hits": cache_hits,
        "est_cost_usd": _est_cost(usage["input_tokens"], usage["output_tokens"],
                                  usage["cache_read_input_tokens"]),
        "scored": scored,
        "ai_correct": ai_hits,
        "hypothesis_correct": hyp_hits,
    }
    return {"rows": rows, "summary": summary}


def render_comparison_table(comparison: dict) -> str:
    rows = comparison["rows"]
    s = comparison["summary"]
    lines = ["trigger_ts            kind             hyp   ai    agree tgt(hyp/ai)                verdict     realized  ai_ok"]
    lines.append("-" * 110)
    for r in rows:
        lines.append(
            f"{str(r['trigger_ts'])[:19]:<21} {str(r['trigger_kind'])[:15]:<16} "
            f"{str(r['hyp_direction']):<5} {str(r['ai_direction']):<5} "
            f"{str(r['direction_agree']):<5} "
            f"{str(r['hyp_target'])[:11] + '/' + str(r['ai_target'])[:11]:<25} "
            f"{str(r['verdict']):<11} {str(r['realized']):<9} {str(r['ai_correct'])}")
    lines.append("-" * 110)
    lines.append(
        f"triggers={s['n_triggers']} dir_agree={s['agree']['direction']} "
        f"dir_disagree={s['disagree']['direction']} mean_latency={s['mean_latency_sec']}s "
        f"api_calls={s['api_calls']} cache_hits={s['cache_hits']} "
        f"est_cost=${s['est_cost_usd']} "
        f"ai_correct={s['ai_correct']}/{s['scored']} hyp_correct={s['hypothesis_correct']}/{s['scored']}")
    return "\n".join(lines)
