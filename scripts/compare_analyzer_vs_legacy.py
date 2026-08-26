"""Side-by-side of the legacy engine's entry intents and the cycle-1 trader's.

READ ONLY on the legacy stream. This module opens the legacy event log for reading and
never for writing — that file is the regression's line-for-line baseline and a single
stray write invalidates every locked comparison. The trader's own decisions come from
`trader_decisions.jsonl`, which only the trader writes.

Both sides are restricted to the RTH window (09:30-16:00 ET), because that is the only
window cycle-1 mechanisms are armed in and comparing outside it would mostly count
overnight legacy activity the trader was never asked to have an opinion about.

CLI:
    python scripts/compare_analyzer_vs_legacy.py <session_dir>
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

LEGACY_LOG = "events" + ".jsonl"          # split so this module never contains a literal
TRADER_LOG = "trader_decisions.jsonl"

WINDOW_START = (9, 30)
WINDOW_END = (16, 0)
TZ = "America/New_York"

# Legacy kinds that express "I intend to enter here".
LEGACY_ENTRY_KINDS = ("new-stop-entry", "move-stop-entry", "market-entry")


def _read_jsonl(path: str) -> list:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:          # read mode only
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _ts(value):
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize(TZ)
    return ts.tz_convert(TZ)


def _in_window(ts) -> bool:
    if ts is None:
        return False
    lo = ts.normalize() + pd.Timedelta(hours=WINDOW_START[0], minutes=WINDOW_START[1])
    hi = ts.normalize() + pd.Timedelta(hours=WINDOW_END[0], minutes=WINDOW_END[1])
    return lo <= ts <= hi


def extract_legacy_intents(session_dir) -> list:
    """Entry intents from the legacy stream, normalised to a common shape.

    Real live records carry `entry_price`; older/synthetic ones carry `price`. Both are
    accepted and surfaced as `price` so the two sides line up column-for-column.
    """
    out = []
    for rec in _read_jsonl(os.path.join(str(session_dir), LEGACY_LOG)):
        if rec.get("kind") not in LEGACY_ENTRY_KINDS:
            continue
        price = rec.get("price")
        if price is None:
            price = rec.get("entry_price", rec.get("new_entry_price"))
        out.append({
            "source": "legacy",
            "kind": rec.get("kind"),
            "time": rec.get("time"),
            "ts": _ts(rec.get("time")),
            "price": price,
            "stop": rec.get("stop_price", rec.get("new_stop_price")),
            "direction": rec.get("direction"),
        })
    return out


def extract_trader_intents(session_dir) -> list:
    out = []
    for rec in _read_jsonl(os.path.join(str(session_dir), TRADER_LOG)):
        if rec.get("kind") != "intended_entry":
            continue
        out.append({
            "source": "trader",
            "kind": rec.get("kind"),
            "time": rec.get("time"),
            "ts": _ts(rec.get("time")),
            "price": rec.get("trigger"),
            "stop": rec.get("stop"),
            "direction": None,
            "mechanism": rec.get("mechanism"),
            "artifact_id": rec.get("artifact_id"),
            "artifact_label": rec.get("artifact_label"),
        })
    return out


def extract_trader_vetoes(session_dir) -> list:
    return [r for r in _read_jsonl(os.path.join(str(session_dir), TRADER_LOG))
            if r.get("kind") == "veto"]


def compare(session_dir) -> dict:
    """`{"legacy": [...], "trader": [...], "vetoes": [...], "window": (...)}`, both
    sides filtered to the RTH window."""
    legacy = [r for r in extract_legacy_intents(session_dir) if _in_window(r["ts"])]
    trader = [r for r in extract_trader_intents(session_dir) if _in_window(r["ts"])]
    vetoes = [r for r in extract_trader_vetoes(session_dir) if _in_window(_ts(r.get("time")))]
    return {
        "session_dir": str(session_dir),
        "window": {"start": "%02d:%02d" % WINDOW_START, "end": "%02d:%02d" % WINDOW_END},
        "legacy": legacy,
        "trader": trader,
        "vetoes": vetoes,
    }


def render(result: dict) -> str:
    lines = [f"session: {result['session_dir']}",
             f"window : {result['window']['start']}-{result['window']['end']} ET", "",
             "| time | side | kind/mechanism | price | stop | note |",
             "|---|---|---|---|---|---|"]
    rows = sorted(result["legacy"] + result["trader"],
                  key=lambda r: (str(r.get("time")), r["source"]))
    for r in rows:
        note = r.get("artifact_label") or r.get("direction") or ""
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            str(r.get("time"))[:19], r["source"],
            r.get("mechanism") or r.get("kind"), r.get("price"), r.get("stop"), note))
    if result["vetoes"]:
        lines += ["", "vetoes:"]
        for v in result["vetoes"]:
            lines.append(f"  {str(v.get('time'))[:19]} {v.get('mechanism')} "
                         f"{v.get('reason')} {v.get('detail')}")
    lines += ["", f"legacy intents: {len(result['legacy'])}  |  "
                  f"trader intents: {len(result['trader'])}  |  "
                  f"trader vetoes: {len(result['vetoes'])}"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = compare(args.session_dir)
    print(json.dumps(result, indent=2, default=str) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
