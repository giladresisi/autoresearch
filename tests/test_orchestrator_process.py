# tests/test_orchestrator_process.py
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from orchestrator.output import OutputChannel
from orchestrator.process import ProcessManager, _kill_existing_signal_smt
from orchestrator.relay import SessionRelay

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
# 7b. test_terminate_reaps_orphan_descendants  (D2: no grandchild left trading)
# ---------------------------------------------------------------------------
def test_terminate_reaps_orphan_descendants():
    """A wrapper subprocess whose real worker is a grandchild must not leave an orphan:
    _terminate captures descendants before killing the parent and reaps any survivors."""
    relay = make_relay()
    log, lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    proc = MagicMock()
    proc.pid = 4242
    proc.wait.return_value = 0

    # Grandchild (the real automation.main worker) still alive after the parent dies.
    orphan = MagicMock()
    orphan.pid = 4243

    with patch("orchestrator.process.psutil") as mock_psutil:
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied
        mock_psutil.Process.return_value.children.return_value = [orphan]
        # wait_procs reports the orphan as still alive after terminate → must be killed.
        mock_psutil.wait_procs.return_value = ([], [orphan])

        pm._terminate(proc)

    proc.terminate.assert_called_once()
    orphan.terminate.assert_called_once()
    orphan.kill.assert_called_once()
    assert any("4243" in line for line in lines)


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


# ---------------------------------------------------------------------------
# 10. test_monitor_returns_ib_disconnected_on_exit_code_2
# ---------------------------------------------------------------------------
def test_monitor_returns_ib_disconnected_on_exit_code_2():
    relay = make_relay()
    log, _lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    proc = make_mock_proc(poll_sequence=[2], returncode=2)
    result = pm._monitor(proc)
    assert result == "ib_disconnected"


# ---------------------------------------------------------------------------
# 11. test_monitor_returns_unexpected_exit_for_other_codes
# ---------------------------------------------------------------------------
def test_monitor_returns_unexpected_exit_for_other_codes():
    relay = make_relay()
    log, _lines = make_log_channel()
    pm = ProcessManager(SCRIPT_PATH, relay, log)

    for code in [1, 3, -1]:
        proc = make_mock_proc(poll_sequence=[code], returncode=code)
        result = pm._monitor(proc)
        assert result == "unexpected_exit", f"Expected unexpected_exit for code {code}"


