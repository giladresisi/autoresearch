# Unit tests for scripts/rebuild_trades_from_events.py — reconstructing a complete
# trade ledger from a session's events.jsonl (GIL-13).
from __future__ import annotations

import json
from pathlib import Path

from scripts.rebuild_trades_from_events import (
    _TSV_HEADERS,
    rebuild_trades_from_events,
    write_trades_tsv,
)


def _write_events(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return p


def test_pairs_and_computes_pnl(tmp_path):
    """A long round-trip and a short round-trip pair correctly with the right P&L,
    and placement / cautious-arming events are ignored (not treated as fills/exits)."""
    events = [
        {"kind": "new-stop-entry", "time": "2026-06-05T01:00:00-04:00", "direction": "long", "entry_price": 30100.0},  # placement — ignored
        {"kind": "stop-entry-filled", "time": "2026-06-05T01:01:00-04:00", "direction": "long", "price": 30100.0, "stop_price": 30080.0},
        {"kind": "move-stop-exit", "time": "2026-06-05T01:02:00-04:00", "direction": "up", "level": "secondary"},  # cautious arm — ignored
        {"kind": "stopped-out", "time": "2026-06-05T01:03:00-04:00", "direction": "up", "price": 30080.0},
        {"kind": "stop-entry-filled", "time": "2026-06-05T02:00:00-04:00", "direction": "short", "price": 30050.0, "stop_price": 30070.0},
        {"kind": "market-close", "time": "2026-06-05T02:05:00-04:00", "direction": "down", "price": 30030.0, "reason": "user-requested"},
    ]
    trades = rebuild_trades_from_events(_write_events(tmp_path, events), contracts=2)

    assert len(trades) == 2
    # Long: (30080 - 30100) * +1 * 2 * 2.0 = -80.0
    assert trades[0]["direction"] == "long"
    assert trades[0]["entry_price"] == 30100.0
    assert trades[0]["exit_reason"] == "stopped-out"
    assert trades[0]["pnl_dollars"] == -80.0
    assert trades[0]["pnl_points"] == -20.0
    # Short: (30030 - 30050) * -1 * 2 * 2.0 = +80.0
    assert trades[1]["direction"] == "short"
    assert trades[1]["exit_reason"] == "market-close"
    assert trades[1]["pnl_dollars"] == 80.0


def test_unpaired_entry_is_flagged_open(tmp_path):
    """A fill with no following exit (e.g. a phantom) is emitted as unpaired-open, not dropped."""
    events = [
        {"kind": "stop-entry-filled", "time": "2026-06-05T04:21:54-04:00", "direction": "long", "price": 30170.75, "stop_price": 30147.75},
    ]
    trades = rebuild_trades_from_events(_write_events(tmp_path, events), contracts=2)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "unpaired-open"
    assert trades[0]["exit_time"] == "" and trades[0]["exit_price"] == ""
    assert trades[0]["pnl_dollars"] == "" and trades[0]["direction"] == "long"


def test_entry_while_open_flushes_prior_as_unpaired(tmp_path):
    """A second fill before any exit flushes the first as unpaired (stacked/phantom fill),
    then the second pairs with the following exit."""
    events = [
        {"kind": "stop-entry-filled", "time": "2026-06-05T01:00:00-04:00", "direction": "long", "price": 30100.0},
        {"kind": "stop-entry-filled", "time": "2026-06-05T01:05:00-04:00", "direction": "long", "price": 30110.0},
        {"kind": "stopped-out", "time": "2026-06-05T01:06:00-04:00", "direction": "up", "price": 30090.0},
    ]
    trades = rebuild_trades_from_events(_write_events(tmp_path, events), contracts=2)
    assert len(trades) == 2
    assert trades[0]["exit_reason"] == "unpaired-open" and trades[0]["entry_price"] == 30100.0
    assert trades[1]["exit_reason"] == "stopped-out" and trades[1]["entry_price"] == 30110.0


def test_bad_entry_price_is_flagged_suspect_no_pnl(tmp_path):
    """A market-entry recorded as 0.0 (the 2026-06-05 10:02 case) must NOT produce a nonsense
    P&L — it's tagged suspect:bad-price with blank P&L instead."""
    events = [
        {"kind": "market-entry", "time": "2026-06-05T10:02:47-04:00", "direction": "long", "price": 0.0},
        {"kind": "stopped-out", "time": "2026-06-05T10:06:36-04:00", "direction": "up", "price": 29771.0},
    ]
    trades = rebuild_trades_from_events(_write_events(tmp_path, events), contracts=2)
    assert len(trades) == 1
    assert "suspect:bad-price" in trades[0]["exit_reason"]
    assert trades[0]["pnl_dollars"] == "" and trades[0]["pnl_points"] == ""


def test_long_hold_is_flagged_suspect_no_pnl(tmp_path):
    """A fill whose only following exit is hours later (a phantom that absorbed a later
    stop-out, like 2026-06-05 04:21 -> 09:28) is tagged suspect:long-hold, P&L blanked."""
    events = [
        {"kind": "stop-entry-filled", "time": "2026-06-05T04:21:54-04:00", "direction": "long", "price": 30170.75},
        {"kind": "stopped-out", "time": "2026-06-05T09:28:35-04:00", "direction": "up", "price": 30025.25},
    ]
    trades = rebuild_trades_from_events(_write_events(tmp_path, events), contracts=2)
    assert len(trades) == 1
    assert "suspect:long-hold" in trades[0]["exit_reason"]
    assert trades[0]["pnl_dollars"] == ""


def test_orphan_exit_skipped_and_tsv_roundtrips(tmp_path):
    """An exit with no open entry is skipped; the TSV writer emits the exact schema header."""
    events = [
        {"kind": "stopped-out", "time": "2026-06-05T00:30:00-04:00", "direction": "up", "price": 30000.0},  # orphan — skipped
        {"kind": "market-entry", "time": "2026-06-05T01:00:00-04:00", "direction": "short", "price": 30050.0},
        {"kind": "stopped-out", "time": "2026-06-05T01:02:00-04:00", "direction": "down", "price": 30070.0},
    ]
    trades = rebuild_trades_from_events(_write_events(tmp_path, events), contracts=1)
    assert len(trades) == 1
    assert trades[0]["direction"] == "short"
    # short stop-out: (30070 - 30050) * -1 * 1 * 2.0 = -40.0
    assert trades[0]["pnl_dollars"] == -40.0

    out = tmp_path / "trades_full.tsv"
    write_trades_tsv(trades, out)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "\t".join(_TSV_HEADERS)
    assert len(lines) == 2  # header + 1 trade
