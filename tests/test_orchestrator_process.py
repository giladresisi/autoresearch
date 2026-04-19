# tests/test_orchestrator_process.py
import datetime
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from orchestrator.output import OutputChannel
from orchestrator.process import ProcessManager, _kill_existing_signal_smt
from orchestrator.relay import SessionRelay

_ET = __import__("zoneinfo").ZoneInfo("America/New_York")

SCRIPT_PATH = Path("signal_smt.py")


def make_mock_proc(poll_sequence, stdout_lines=None, returncode=0, pid=1234):
    """poll_sequence: list of return values for successive poll() calls"""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    poll_iter = iter(poll_sequence)
    proc.poll.side_effect = lambda: next(poll_iter, returncode)
    if stdout_lines is not None:
        proc.stdout = iter(line + "\n" for line in stdout_lines)
    else:
        proc.stdout = iter([])
    return proc


def make_log_channel():
    log = OutputChannel()
    lines = []
    sink = MagicMock()
    sink.write.side_effect = lambda t: lines.append(t)
    log.add_sink(sink)
    return log, lines


def make_relay():
    channel = OutputChannel()
    return SessionRelay(channel)


class _FakeDateTime:
    """Stand-in for datetime.datetime that returns a fixed now() but proxies other attributes."""

    def __init__(self, fixed_now: datetime.datetime):
        self._fixed = fixed_now

    def now(self, tz=None):
        if tz is not None:
            return self._fixed.astimezone(tz) if self._fixed.tzinfo else self._fixed.replace(tzinfo=tz)
        return self._fixed


def _patch_datetime_at(hour: int, minute: int):
    """Return a patch context that forces datetime.datetime.now(tz=_ET) to return the given ET time."""
    fixed = datetime.datetime(2026, 4, 19, hour, minute, 0, tzinfo=_ET)
    fake = _FakeDateTime(fixed)
    return patch("orchestrator.process.datetime.datetime", fake)


# ---------------------------------------------------------------------------
# 1. test_run_session_spawns_subprocess
# ---------------------------------------------------------------------------
def test_run_session_spawns_subprocess():
    relay = make_relay()
    log, _lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    proc = make_mock_proc(poll_sequence=[None])  # running when _monitor starts
    with patch("orchestrator.process.subprocess.Popen", return_value=proc) as mock_popen, \
         patch("orchestrator.process._kill_existing_signal_smt"), \
         patch("orchestrator.process.time.sleep"), \
         _patch_datetime_at(13, 36):
        pm.run_session(datetime.date(2026, 4, 19))

    mock_popen.assert_called_once()
    call_args = mock_popen.call_args
    assert call_args.args[0] == [sys.executable, str(SCRIPT_PATH)]


# ---------------------------------------------------------------------------
# 2. test_run_session_relays_stdout_lines
# ---------------------------------------------------------------------------
def test_run_session_relays_stdout_lines():
    channel = OutputChannel()
    relay = SessionRelay(channel)
    relay.emit = MagicMock(wraps=relay.emit)
    log, _lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    proc = make_mock_proc(
        poll_sequence=[None, None],
        stdout_lines=["hello", "world"],
    )
    with patch("orchestrator.process.subprocess.Popen", return_value=proc), \
         patch("orchestrator.process._kill_existing_signal_smt"), \
         patch("orchestrator.process.time.sleep"), \
         _patch_datetime_at(13, 36):
        pm.run_session(datetime.date(2026, 4, 19))

    # The reader thread consumes stdout and calls relay.emit for each line.
    assert relay.emit.call_count == 2
    called_args = [c.args[0] for c in relay.emit.call_args_list]
    assert "hello" in called_args
    assert "world" in called_args


# ---------------------------------------------------------------------------
# 3. test_run_session_scheduled_stop_terminates
# ---------------------------------------------------------------------------
def test_run_session_scheduled_stop_terminates():
    relay = make_relay()
    log, lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    # poll() always returns None → process "still running" → scheduled_stop triggers terminate.
    proc = MagicMock()
    proc.pid = 555
    proc.returncode = 0
    proc.poll.return_value = None
    proc.stdout = iter([])
    proc.wait.return_value = 0

    with patch("orchestrator.process.subprocess.Popen", return_value=proc), \
         patch("orchestrator.process._kill_existing_signal_smt"), \
         patch("orchestrator.process.time.sleep"), \
         _patch_datetime_at(13, 36):
        pm.run_session(datetime.date(2026, 4, 19))

    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# 4. test_run_session_unexpected_exit_restarts_once
# ---------------------------------------------------------------------------
def test_run_session_unexpected_exit_restarts_once():
    relay = make_relay()
    log, lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    # First proc: poll() returns 1 immediately (unexpected exit).
    proc1 = MagicMock()
    proc1.pid = 1
    proc1.returncode = 1
    proc1.poll.return_value = 1
    proc1.stdout = iter([])

    # Second proc: scheduled_stop.
    proc2 = MagicMock()
    proc2.pid = 2
    proc2.returncode = 0
    proc2.poll.return_value = None
    proc2.stdout = iter([])
    proc2.wait.return_value = 0

    procs = [proc1, proc2]
    with patch("orchestrator.process.subprocess.Popen", side_effect=procs) as mock_popen, \
         patch("orchestrator.process._kill_existing_signal_smt"), \
         patch("orchestrator.process.time.sleep"), \
         _patch_datetime_at(13, 36):
        pm.run_session(datetime.date(2026, 4, 19))

    assert mock_popen.call_count == 2
    combined = "".join(lines)
    assert "restarting once" in combined


# ---------------------------------------------------------------------------
# 5. test_run_session_second_exit_no_restart
# ---------------------------------------------------------------------------
def test_run_session_second_exit_no_restart():
    relay = make_relay()
    log, lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    proc1 = MagicMock()
    proc1.pid = 1
    proc1.returncode = 1
    proc1.poll.return_value = 1
    proc1.stdout = iter([])

    proc2 = MagicMock()
    proc2.pid = 2
    proc2.returncode = 1
    proc2.poll.return_value = 1
    proc2.stdout = iter([])

    procs = [proc1, proc2]
    with patch("orchestrator.process.subprocess.Popen", side_effect=procs) as mock_popen, \
         patch("orchestrator.process._kill_existing_signal_smt"), \
         patch("orchestrator.process.time.sleep"), \
         _patch_datetime_at(13, 36):
        pm.run_session(datetime.date(2026, 4, 19))

    assert mock_popen.call_count == 2
    combined = "".join(lines)
    assert "NOT restarting" in combined


# ---------------------------------------------------------------------------
# 6. test_terminate_calls_kill_on_timeout
# ---------------------------------------------------------------------------
def test_terminate_calls_kill_on_timeout():
    relay = make_relay()
    log, lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    proc = MagicMock()
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="signal_smt.py", timeout=10)

    pm._terminate(proc)

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    combined = "".join(lines)
    assert "SIGTERM timeout" in combined


# ---------------------------------------------------------------------------
# 7. test_terminate_graceful
# ---------------------------------------------------------------------------
def test_terminate_graceful():
    relay = make_relay()
    log, _lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    proc = MagicMock()
    proc.wait.return_value = 0

    pm._terminate(proc)

    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# 8. test_kill_existing_terminates_matching_process
# ---------------------------------------------------------------------------
def test_kill_existing_terminates_matching_process():
    log, lines = make_log_channel()

    mock_proc = MagicMock()
    mock_proc.info = {"pid": 999, "cmdline": ["python", "signal_smt.py"]}
    mock_proc.pid = 999

    with patch("orchestrator.process.psutil") as mock_psutil:
        mock_psutil.process_iter.return_value = [mock_proc]
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied
        mock_psutil.TimeoutExpired = psutil.TimeoutExpired
        _kill_existing_signal_smt(SCRIPT_PATH, log)

    mock_proc.terminate.assert_called_once()
    combined = "".join(lines)
    assert "Killing existing signal_smt.py" in combined


# ---------------------------------------------------------------------------
# 9. test_kill_existing_skips_non_matching_process
# ---------------------------------------------------------------------------
def test_kill_existing_skips_non_matching_process():
    log, _lines = make_log_channel()

    mock_proc = MagicMock()
    mock_proc.info = {"pid": 999, "cmdline": ["python", "other_script.py"]}
    mock_proc.pid = 999

    with patch("orchestrator.process.psutil") as mock_psutil:
        mock_psutil.process_iter.return_value = [mock_proc]
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied
        mock_psutil.TimeoutExpired = psutil.TimeoutExpired
        _kill_existing_signal_smt(SCRIPT_PATH, log)

    mock_proc.terminate.assert_not_called()
