"""Acceptance gate 1 — facts parity of the ONLINE assembler vs the OFFLINE reference.

What this actually tests. `agent.facts.assemble` deliberately routes the L1 view through
`derive_facts.compute_facts` and `agent.bench.facts.bundle_to_l1_view` — the same code
the offline reference uses — so the S0–S9 CONTENT is equal by construction. What is NOT
equal by construction, and is exactly what breaks silently, is the **input preparation**:

  - the 17-day primary slice and its `< boundary` (strictly-before) cut,
  - the CME-maintenance filter and its inclusive bounds,
  - which frame the all-time high is taken from (RAW, maintenance bars included),
  - the full-history `hist_*` frames handed to `compute_facts`.

So the harness feeds the online path the RAW parquet frames and compares its rendered
S0–S7 sheet, its hash, and every `DECIDE_THESIS_KEYS` value against
`ParquetFactsSource.build_facts(boundary)`.

S8/S9 (`menu_text` / `evidence_text`) are deliberately EXCLUDED from the hashed identity
(`agent/derive_facts.py` `facts_to_validator_dict` docstring), so they are compared
separately and reported as advisory.

CLI:
    python scripts/facts_parity.py --dates 2026-07-14,2026-07-15,...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.bench.facts import ParquetFactsSource, bundle_to_l1_view      # noqa: E402
from agent.facts.assemble import DECIDE_THESIS_KEYS, build_bundle        # noqa: E402
from agent.facts.requirements import ANALYZER_REQUIREMENT                # noqa: E402
from derive_facts import render_facts_text                               # noqa: E402

TZ = "America/New_York"

# rerun_finalfinal's 10-day 09:20 panel.
PANEL_DATES = ("2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
               "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23",
               "2026-07-24", "2026-07-27")

ARM = "09:20"

# The window comes from the requirement, so a change to it fails the gate loudly instead
# of silently comparing two different views.
LOOKBACK = ANALYZER_REQUIREMENT.windows[list(ANALYZER_REQUIREMENT.windows)[0]]

_SOURCE = None


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def get_source(main_dir: "str | None" = None) -> ParquetFactsSource:
    """Module-level cache — the 1s parquets are ~200 MB and take seconds to load."""
    global _SOURCE
    if _SOURCE is None or main_dir is not None:
        _SOURCE = (ParquetFactsSource(main_dir) if main_dir
                   else ParquetFactsSource())
    return _SOURCE


def _raw_bars(source) -> dict:
    raw = getattr(source, "_raw_1s", None) or {}
    return {tk: raw.get(tk) for tk in ("MNQ", "MES")}


def _diff(key: str, expected, got) -> dict:
    return {"key": key,
            "reference": _short(expected),
            "online": _short(got)}


def _short(v, cap: int = 400) -> str:
    try:
        s = json.dumps(v, default=str, sort_keys=True)
    except Exception:
        s = str(v)
    return s if len(s) <= cap else s[:cap] + f"...<{len(s)} chars>"


def compare_at(boundary: pd.Timestamp, source=None, main_dir: "str | None" = None) -> dict:
    """Compare the online assembler against the offline reference at one boundary.

    Returns `{"equal": bool, "diffs": [...], "advisory": [...], ...}`. Never raises: a
    missing/unreadable data root is reported as a structured diff so the gate can record
    it rather than crash.
    """
    out = {"boundary": str(boundary), "equal": False, "diffs": [], "advisory": []}
    try:
        src = source if source is not None else get_source(main_dir)
    except Exception as exc:
        out["diffs"].append(_diff("_source", "loadable", f"{type(exc).__name__}: {exc}"))
        out["error"] = "source-unavailable"
        return out

    try:
        ref = src.build_facts(boundary)
    except Exception as exc:
        out["diffs"].append(_diff("_reference", "built", f"{type(exc).__name__}: {exc}"))
        out["error"] = "reference-failed"
        return out
    if getattr(ref, "degraded", False):
        out["diffs"].append(_diff("_reference", "not-degraded", ref.error))
        out["error"] = f"reference-degraded:{ref.error}"
        return out

    try:
        bundle = build_bundle(_raw_bars(src), boundary)
        if bundle is None:
            out["diffs"].append(_diff("_online", "built", "degraded/None"))
            out["error"] = "online-degraded"
            return out
        text = render_facts_text(bundle)
        vd, menu_text, evidence_text, magnitude = bundle_to_l1_view(bundle)
    except Exception as exc:
        out["diffs"].append(_diff("_online", "built", f"{type(exc).__name__}: {exc}"))
        out["error"] = "online-failed"
        return out

    # --- hashed S0-S7 identity first ---------------------------------------- #
    out["reference_hash"] = ref.content_hash
    out["online_hash"] = _sha(text)
    out["hash_equal"] = (out["reference_hash"] == out["online_hash"])
    if not out["hash_equal"]:
        out["diffs"].append(_diff("_s0_s7_hash", ref.content_hash, out["online_hash"]))
        out["first_text_divergence"] = _first_line_divergence(ref.text, text)

    # --- then key by key over the decide_thesis contract ---------------------- #
    for key in DECIDE_THESIS_KEYS:
        a, b = ref.validator_dict.get(key), vd.get(key)
        if _short(a, 10 ** 9) != _short(b, 10 ** 9):
            out["diffs"].append(_diff(key, a, b))

    if _short(ref.evidence_magnitude, 10 ** 9) != _short(magnitude, 10 ** 9):
        out["diffs"].append(_diff("evidence_magnitude", ref.evidence_magnitude, magnitude))

    # --- S8/S9 are deliberately unhashed -> advisory -------------------------- #
    if (ref.menu_text or "") != (menu_text or ""):
        out["advisory"].append(_diff("menu_text(S8)", ref.menu_text, menu_text))
    if (ref.evidence_text or "") != (evidence_text or ""):
        out["advisory"].append(_diff("evidence_text(S9)", ref.evidence_text, evidence_text))

    out["equal"] = not out["diffs"]
    return out


def _first_line_divergence(a: str, b: str) -> "dict | None":
    la, lb = (a or "").split("\n"), (b or "").split("\n")
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<missing>"
        y = lb[i] if i < len(lb) else "<missing>"
        if x != y:
            return {"line": i + 1, "reference": x[:200], "online": y[:200]}
    return None


def run_panel(dates=PANEL_DATES, arm: str = ARM, main_dir: "str | None" = None) -> list:
    rows = []
    for d in dates:
        boundary = pd.Timestamp(f"{d} {arm}", tz=TZ)
        rows.append(compare_at(boundary, main_dir=main_dir))
    return rows


def _render(rows) -> str:
    lines = ["| date | hash equal | contract diffs | advisory | note |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {} | {} | {} | {} | {} |".format(
            str(r.get("boundary"))[:16],
            "YES" if r.get("hash_equal") else "no",
            ", ".join(d["key"] for d in r["diffs"]) or "-",
            ", ".join(d["key"] for d in r["advisory"]) or "-",
            r.get("error", "")))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dates", default=",".join(PANEL_DATES))
    ap.add_argument("--arm", default=ARM)
    ap.add_argument("--main-dir", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = run_panel(tuple(d.strip() for d in args.dates.split(",") if d.strip()),
                     arm=args.arm, main_dir=args.main_dir)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_render(rows))
        for r in rows:
            for d in r["diffs"]:
                print(f"\nDIFF {r['boundary']} {d['key']}\n  ref: {d['reference']}"
                      f"\n  new: {d['online']}")
    return 0 if all(r.get("equal") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
