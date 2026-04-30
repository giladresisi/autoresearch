# tests/test_orchestrator_relay.py
import datetime
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.output import OutputChannel
from orchestrator.relay import SessionRelay


# Legacy format (no entry_time) — must still parse for backward compatibility
SIGNAL_LONG = "[09:14:32] SIGNAL    long  | entry ~19850.75 (+2t slip) | stop 19848.50 | TP 19890.00 | RR ~24.0x"
SIGNAL_SHORT = "[09:14:32] SIGNAL    short | entry ~19850.75 (+2t slip) | stop 19848.50 | TP 19890.00 | RR ~24.0x"
# Current format (with entry_time)
SIGNAL_LONG_NEW = "[09:14:32] SIGNAL    long  | entry_time 09:12:00 | entry ~19850.75 (+2t slip) | stop 19848.50 | TP 19890.00 | RR ~24.0x"
EXIT_TP = "[09:47:11] EXIT      tp    | filled 19890.00 | P&L +$78.50 | 1 MNQ1! contract"
EXIT_STOP = "[09:47:11] EXIT      stop  | filled 19890.00 | P&L +$78.50 | 1 MNQ1! contract"
EXIT_NEG = "[09:47:11] EXIT      stop  | filled 19890.00 | P&L -$3.00 | 1 MNQ1! contract"
STOP_MOVE = "[09:30:00] STOP_MOVE breakeven   | stop 19848.50 -> 19850.75"


def _make():
    channel = MagicMock(spec=OutputChannel)
    return channel, SessionRelay(channel)


def test_emit_signal_line_writes_to_channel():
    channel, relay = _make()
    relay.emit(SIGNAL_LONG)
    channel.write.assert_called_once_with(SIGNAL_LONG + "\n")


def test_emit_signal_line_parses_long():
    channel, relay = _make()
    relay.emit(SIGNAL_LONG)
    events = relay.get_events()
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "SIGNAL"
    assert e["direction"] == "long"
    assert e["entry"] == 19850.75
    assert e["stop"] == 19848.50
    assert e["tp"] == 19890.00
    assert e["rr"] == 24.0
    assert e["time"] == "09:14:32"


def test_emit_signal_line_parses_short():
    channel, relay = _make()
    relay.emit(SIGNAL_SHORT)
    events = relay.get_events()
    assert len(events) == 1
    assert events[0]["direction"] == "short"


def test_emit_exit_tp_parses():
    channel, relay = _make()
    relay.emit(EXIT_TP)
    events = relay.get_events()
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "EXIT"
    assert e["exit_kind"] == "tp"
    assert e["pnl"] == 78.50
    assert e["filled"] == 19890.00
    assert e["contracts"] == 1


def test_emit_exit_stop_parses():
    channel, relay = _make()
    relay.emit(EXIT_STOP)
    events = relay.get_events()
    assert len(events) == 1
    assert events[0]["exit_kind"] == "stop"


def test_emit_exit_negative_pnl_parses():
    channel, relay = _make()
    relay.emit(EXIT_NEG)
    events = relay.get_events()
    assert len(events) == 1
    assert events[0]["pnl"] == -3.00


def test_emit_non_signal_line_no_event():
    channel, relay = _make()
    line = "[09:00:00] status: warmup complete"
    relay.emit(line)
    assert relay.get_events() == []
    channel.write.assert_called_once_with(line + "\n")


def test_emit_stop_move_line_no_event():
    channel, relay = _make()
    relay.emit(STOP_MOVE)
    assert relay.get_events() == []
    channel.write.assert_called_once_with(STOP_MOVE + "\n")


def test_emit_malformed_signal_no_crash():
    channel, relay = _make()
    relay.emit("[09:14:32] SIGNAL long")
    assert relay.get_events() == []


def test_reset_clears_events():
    channel, relay = _make()
    relay.emit(SIGNAL_LONG)
    relay.emit(EXIT_TP)
    assert len(relay.get_events()) == 2
    relay.reset()
    assert relay.get_events() == []


def test_emit_adds_newline_if_missing():
    channel, relay = _make()
    line = "plain status"
    relay.emit(line)
    channel.write.assert_called_once_with(line + "\n")


def test_emit_signal_with_entry_time_parses():
    channel, relay = _make()
    relay.emit(SIGNAL_LONG_NEW)
    events = relay.get_events()
    assert len(events) == 1
    e = events[0]
    assert e["entry_time"] == "09:12:00"
    assert e["entry"] == 19850.75
    assert e["direction"] == "long"


def test_emit_signal_legacy_no_entry_time_key():
    channel, relay = _make()
    relay.emit(SIGNAL_LONG)
    events = relay.get_events()
    assert len(events) == 1
    assert "entry_time" not in events[0]
    assert events[0]["entry"] == 19850.75


def test_write_trades_tsv_no_trades(tmp_path: Path):
    _, relay = _make()
    path = tmp_path / "trades.tsv"
    relay.write_trades_tsv(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["\t".join(["entry_time", "entry_price", "direction", "contracts",
                                "exit_time", "exit_price", "exit_reason", "pnl_points", "pnl_dollars"])]


def test_write_trades_tsv_one_trade(tmp_path: Path):
    _, relay = _make()
    relay.emit(SIGNAL_LONG_NEW)
    relay.emit(EXIT_TP)
    path = tmp_path / "trades.tsv"
    relay.write_trades_tsv(path, datetime.date(2026, 4, 28))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = dict(zip(lines[0].split("\t"), lines[1].split("\t")))
    assert row["entry_time"] == "2026-04-28T09:12:00"
    assert row["exit_time"] == "2026-04-28T09:47:11"
    assert row["direction"] == "long"
    assert row["exit_reason"] == "tp"
    assert float(row["pnl_dollars"]) == 78.50
    assert float(row["pnl_points"]) == 78.50 / (1 * 2.0)


def test_write_trades_tsv_unpaired_signal_skipped(tmp_path: Path):
    _, relay = _make()
    relay.emit(SIGNAL_LONG_NEW)
    # No EXIT emitted
    path = tmp_path / "trades.tsv"
    relay.write_trades_tsv(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # header only
