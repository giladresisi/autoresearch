#!/usr/bin/env python
"""Reconstruct a complete trade ledger from a session's events.jsonl.

The live `trades.tsv` (`orchestrator/relay.py::write_trades_tsv`) is built from the relay's
in-memory, per-run events and OVERWRITTEN at each session-end — so it loses round-trips
across restarts/terminations (only the final clean run-segment survives). `events.jsonl` is
the persistent, append-only record of every fill and exit across ALL run segments, so the
complete ledger can be rebuilt from it.

This is a read-only, retrospective reconstruction — it touches no live-trading code. It
mirrors the `trades.tsv` / `trades_1s.tsv` schema so session-analysis can compare
like-for-like.

It is best-effort and ANOMALY-FLAGGING rather than perfect: a multi-restart session can
contain a phantom fill with no exit event, an inverted fill/stop-out timestamp, or a bad
(<=0) recorded price. Rather than silently emitting nonsense P&L, suspect pairings get their
P&L blanked and a `|suspect:<why>` token appended to `exit_reason`:
  - `bad-price`  — entry or exit price <= 0 (e.g. a manual market-entry logged as 0.0)
  - `long-hold`  — held longer than _MAX_HOLD_MINUTES (almost certainly a missing exit event
                   or a cross-restart mispair, e.g. a phantom that absorbed a later stop-out)
Unpaired entries (a fill with no matching exit) are emitted with `exit_reason="unpaired-open"`
and blank exit fields, so phantoms surface rather than vanish. The printed P&L total sums
ONLY clean (numeric-P&L) trades.

CLI:
    uv run python -m scripts.rebuild_trades_from_events --date 2026-06-05
        -> writes <sessions_dir>/<date>/trades_full.tsv (the original trades.tsv is left intact)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths  # noqa: E402

# Kept in sync with orchestrator/relay.py (the live trades.tsv writer) so the reconstructed
# ledger is directly comparable.
_MNQ_PNL_PER_POINT = 2.0
_TSV_HEADERS = [
    "entry_time", "entry_price", "direction", "contracts",
    "exit_time", "exit_price", "exit_reason", "pnl_points", "pnl_dollars",
]

# A paired hold longer than this is almost certainly a missing exit event / cross-restart
# mispair (the phantom that absorbed a later stop-out), not a real position — flag it.
_MAX_HOLD_MINUTES = 180.0

# events.jsonl kinds that represent an actual FILL (entry) vs an EXIT. Placement kinds
# (new-stop-entry / move-stop-entry, incl. the stp_mkt_downgrade placement) are NOT fills and
# are ignored — only the fill itself counts, so an instant STP->MKT downgrade (which logs both
# the placement and the fill) is not double-counted. Cautious-arming events
# (new-stop-exit / move-stop-exit) are not exits — only the realized exit kinds below are.
_ENTRY_KINDS = {"stop-entry-filled", "market-entry"}
_EXIT_KINDS = {"stopped-out", "market-close", "stop-exit"}


def _norm_dir(direction: str) -> str:
    """Normalize a direction to long/short (entries use long/short, exits use up/down)."""
    d = (direction or "").lower()
    if d in ("long", "up"):
        return "long"
    if d in ("short", "down"):
        return "short"
    return d


def _parse_ts(s: str) -> "datetime.datetime | None":
    try:
        return datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _hold_minutes(entry_ts: str, exit_ts: str) -> "float | None":
    a, b = _parse_ts(entry_ts), _parse_ts(exit_ts)
    if a is None or b is None:
        return None
    return abs((b - a).total_seconds()) / 60.0


def rebuild_trades_from_events(events_path: Path, *, contracts: int | None = None) -> list[dict]:
    """Pair fills -> next exit (FIFO) across the whole session; return trade dicts.

    The strategy holds at most one position at a time, so a single open-position state
    machine reconstructs the round-trips. An entry arriving while a position is already open
    flushes the prior one as `unpaired-open` (the prior fill never got an exit event — a
    stacked/phantom fill). A position still open at end-of-file is emitted as unpaired too.
    Orphan exits (an exit with no open entry) are skipped. Suspect pairings (bad price or an
    implausibly long hold) keep the row but blank the P&L and tag `exit_reason`.
    """
    if contracts is None:
        contracts = int(os.environ.get("TRADING_CONTRACTS", "2"))

    events: list[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # events.jsonl is append-order (chronological); sort by time defensively.
    events.sort(key=lambda e: e.get("time", ""))

    trades: list[dict] = []
    open_entry: dict | None = None

    def _flush_unpaired(entry: dict) -> None:
        trades.append({
            "entry_time":  entry["time"],
            "entry_price": entry["price"],
            "direction":   entry["dir"],
            "contracts":   contracts,
            "exit_time":   "",
            "exit_price":  "",
            "exit_reason": "unpaired-open",
            "pnl_points":  "",
            "pnl_dollars": "",
        })

    for e in events:
        kind = e.get("kind")
        if kind in _ENTRY_KINDS:
            entry = {
                "time":  e.get("time", ""),
                # stop-entry-filled logs the fill under "price"; market-entry logs it under
                # "entry_price". Read both so a market-entry isn't mis-flagged suspect:bad-price
                # with a 0.0 entry (D2, 2026-06-22 10:06 re-entry). A genuinely missing/0 price
                # still falls through to 0.0 and is flagged suspect downstream.
                "price": float(e.get("price") or e.get("entry_price") or 0.0),
                "dir":   _norm_dir(e.get("direction", "")),
            }
            if open_entry is not None:
                _flush_unpaired(open_entry)
            open_entry = entry
        elif kind in _EXIT_KINDS:
            if open_entry is None:
                continue  # orphan exit — no matching entry
            entry_price = open_entry["price"]
            exit_price = float(e.get("price", 0.0))
            direction = open_entry["dir"]

            # Decide whether this pairing is trustworthy.
            suspect = []
            if entry_price <= 0 or exit_price <= 0:
                suspect.append("bad-price")
            hold = _hold_minutes(open_entry["time"], e.get("time", ""))
            if hold is not None and hold > _MAX_HOLD_MINUTES:
                suspect.append("long-hold")

            exit_reason = kind
            if suspect:
                exit_reason = f"{kind}|suspect:{'+'.join(suspect)}"
                pnl_pts = pnl_dollars = ""
            else:
                sign = 1.0 if direction == "long" else -1.0
                pnl_dollars = round((exit_price - entry_price) * sign * contracts * _MNQ_PNL_PER_POINT, 2)
                pnl_pts = round(pnl_dollars / (contracts * _MNQ_PNL_PER_POINT), 4)

            trades.append({
                "entry_time":  open_entry["time"],
                "entry_price": entry_price,
                "direction":   direction,
                "contracts":   contracts,
                "exit_time":   e.get("time", ""),
                "exit_price":  exit_price,
                "exit_reason": exit_reason,
                "pnl_points":  pnl_pts,
                "pnl_dollars": pnl_dollars,
            })
            open_entry = None

    if open_entry is not None:
        _flush_unpaired(open_entry)

    return trades


def write_trades_tsv(trades: list[dict], out_path: Path) -> None:
    """Write the reconstructed ledger in the trades.tsv schema."""
    lines = ["\t".join(_TSV_HEADERS)]
    for t in trades:
        lines.append("\t".join(str(t[h]) for h in _TSV_HEADERS))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_clean(t: dict) -> bool:
    return isinstance(t["pnl_dollars"], (int, float))


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild a complete trades_full.tsv from a session's events.jsonl")
    ap.add_argument("--date", required=True, help="Session date YYYY-MM-DD")
    ap.add_argument("--events", default=None, help="events.jsonl path (default: <sessions>/<date>/events.jsonl)")
    ap.add_argument("--out", default=None, help="output TSV path (default: <sessions>/<date>/trades_full.tsv)")
    args = ap.parse_args()

    sess = paths.sessions_dir() / args.date
    events_path = Path(args.events) if args.events else sess / "events.jsonl"
    out_path = Path(args.out) if args.out else sess / "trades_full.tsv"
    if not events_path.exists():
        print(f"events.jsonl not found: {events_path}", file=sys.stderr)
        sys.exit(1)

    trades = rebuild_trades_from_events(events_path)
    write_trades_tsv(trades, out_path)

    clean = [t for t in trades if _is_clean(t)]
    suspect = sum(1 for t in trades if "suspect:" in str(t["exit_reason"]))
    unpaired = sum(1 for t in trades if t["exit_reason"] == "unpaired-open")
    total = round(sum(float(t["pnl_dollars"]) for t in clean), 2) if clean else 0.0
    print(f"Wrote {len(trades)} trades ({len(clean)} clean, {suspect} suspect, {unpaired} unpaired-open) "
          f"to {out_path} | clean strategy assumed P&L = ${total}")


if __name__ == "__main__":
    main()
