"""Offline-vs-online facts consistency check (GIL-44 Phase 4, Wave 4.3).

Re-derives the facts OFFLINE over the finished session frames at each trigger's `now`
and diffs the resulting content_hash against the online snapshot hash logged during the
session. Any mismatch flags convention drift between the two paths (the mandated drift
catch, and the safety net for HOLE H1 — recompute-from-frames). On a mismatch the online
snapshot text (stored in the audit) is parsed and diffed against the offline view so the
differing section is named, not just flagged.
"""

from __future__ import annotations

import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_CALIB = os.path.join(os.path.dirname(_AGENT), "calibration")
for _p in (_HERE, _AGENT, _CALIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from facts_adapter import build_snapshot  # noqa: E402
from records import read_audit_records, resolve_snapshot  # noqa: E402


def _diff_sections(online_text: str, offline_validator_dict: dict) -> list:
    """Name the top-level fact sections (now_price / checkpoint / levels[...]) that
    differ between the online snapshot text and the offline view."""
    try:
        from validate_results import parse_facts
    except Exception:                                    # pragma: no cover
        return ["<unparseable>"]
    online = parse_facts(online_text or "")
    diffs = []
    if online.get("now_price") != offline_validator_dict.get("now_price"):
        diffs.append("now_price")
    if online.get("checkpoint") != offline_validator_dict.get("checkpoint"):
        diffs.append("checkpoint")
    on_lvls = online.get("levels") or {}
    off_lvls = offline_validator_dict.get("levels") or {}
    for name in sorted(set(on_lvls) | set(off_lvls)):
        if on_lvls.get(name) != off_lvls.get(name):
            diffs.append(f"levels[{name}]")
    return diffs


def check_consistency(audit_path, mnq_frame, mes_frame, *, hist_mnq=None, hist_mes=None,
                      ath_mnq=None, ath_mes=None) -> dict:
    """For each audit record, rebuild the snapshot offline from the finished-session
    frames at the record's trigger `now` and compare content_hash to the logged online
    hash. Returns {n, matches, drift:[{trigger_ts, online_hash, offline_hash, sections}]}.
    """
    records = read_audit_records(audit_path)
    drift = []
    matches = 0
    tz = mnq_frame.index.tz if mnq_frame is not None else None
    for r in records:
        trigger_ts = pd.Timestamp(r.get("trigger_ts"))
        if tz is not None and trigger_ts.tzinfo is not None:
            trigger_ts = trigger_ts.tz_convert(tz)
        is_ckpt = str(r.get("trigger_kind", "")).startswith("checkpoint")
        frames = {"mnq_today": mnq_frame, "mes_today": mes_frame,
                  "hist_mnq": hist_mnq, "hist_mes": hist_mes, "hist_1hr": None,
                  "hist_4hr": None, "ath_mnq": ath_mnq, "ath_mes": ath_mes,
                  "now": trigger_ts}
        snap = build_snapshot(frames, checkpoint=trigger_ts if is_ckpt else None)
        online_hash = r.get("facts_content_hash")
        if snap.content_hash == online_hash:
            matches += 1
            continue
        ref = r.get("facts_snapshot_ref") or {}
        online_text = ""
        try:
            online_text = resolve_snapshot(ref)
        except Exception:
            online_text = ""
        drift.append({
            "trigger_ts": r.get("trigger_ts"),
            "trigger_kind": r.get("trigger_kind"),
            "online_hash": online_hash,
            "offline_hash": snap.content_hash,
            "sections": _diff_sections(online_text, snap.validator_dict),
        })
    return {"n": len(records), "matches": matches, "drift": drift}
