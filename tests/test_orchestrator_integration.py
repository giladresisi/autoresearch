# tests/test_orchestrator_integration.py
# Integration tests: run ProcessManager against a real (temporary) fake script.
# These tests spawn actual subprocesses -- mark with @pytest.mark.integration so they can be deselected.
import datetime
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.output import FileSink, OutputChannel
from orchestrator.process import ProcessManager
from orchestrator.relay import SessionRelay


def _make_fake_script(tmp_path: Path) -> Path:
    """Write a fake signal_smt.py that emits two valid lines then exits 0."""
    script = tmp_path / "fake_signal_smt.py"
    script.write_text(textwrap.dedent("""
        import time, sys
        print("[09:14:32] SIGNAL    long  | entry ~19850.75 (+2t slip) | stop 19848.50 | TP 19890.00 | RR ~24.0x", flush=True)
        time.sleep(0.05)
        print("[09:47:11] EXIT      tp    | filled 19890.00 | P&L +$78.50 | 1 MNQ1! contract", flush=True)
        sys.exit(0)
    """).strip())
    return script


@pytest.mark.integration
def test_integration_relay_captures_events(tmp_path):
    """ProcessManager with fake script: relay captures at least 1 SIGNAL + 1 EXIT event."""
    script = _make_fake_script(tmp_path)
    session_dir = tmp_path / "sessions" / "2026-04-21"
    session_dir.mkdir(parents=True)

    signal_ch = OutputChannel()
    signal_ch.add_sink(FileSink(session_dir / "signals.log"))
    relay = SessionRelay(signal_ch)
    log_ch = OutputChannel()

    with patch("orchestrator.process._kill_existing_signal_smt"):
        with patch.object(ProcessManager, "_wait_until_grace_end"):
            pm = ProcessManager(script, relay, log_ch)
            pm.run_session(datetime.date(2026, 4, 21))

    events = relay.get_events()
    assert len(events) >= 2, f"Expected at least 2 events, got: {events}"
    signal_events = [e for e in events if e["type"] == "SIGNAL"]
    exit_events = [e for e in events if e["type"] == "EXIT"]
    assert len(signal_events) >= 1
    assert len(exit_events) >= 1
    assert signal_events[0]["direction"] == "long"
    assert signal_events[0]["entry"] == 19850.75
    assert exit_events[0]["exit_kind"] == "tp"
    assert exit_events[0]["filled"] == 19890.00


@pytest.mark.integration
def test_integration_signals_log_written(tmp_path):
    """ProcessManager with fake script: signals.log is created and contains both lines."""
    script = _make_fake_script(tmp_path)
    session_dir = tmp_path / "sessions" / "2026-04-21"
    session_dir.mkdir(parents=True)
    log_file = session_dir / "signals.log"

    signal_ch = OutputChannel()
    signal_ch.add_sink(FileSink(log_file))
    relay = SessionRelay(signal_ch)
    log_ch = OutputChannel()

    with patch("orchestrator.process._kill_existing_signal_smt"):
        with patch.object(ProcessManager, "_wait_until_grace_end"):
            pm = ProcessManager(script, relay, log_ch)
            pm.run_session(datetime.date(2026, 4, 21))

    assert log_file.exists(), "signals.log was not created"
    content = log_file.read_text(encoding="utf-8")
    assert "SIGNAL" in content
    assert "EXIT" in content
    assert "19850.75" in content
    assert "19890.00" in content
