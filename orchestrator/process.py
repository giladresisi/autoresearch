# orchestrator/process.py
# Manages the signal_smt.py subprocess lifecycle: spawn, stdout relay, restart-on-crash, and scheduled stop.
import datetime
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil

from orchestrator.output import OutputChannel
from orchestrator.relay import SessionRelay

_ET = ZoneInfo("America/New_York")
_SESSION_GRACE_END = datetime.time(13, 35)
_SIGTERM_WAIT_S = 10
_POLL_INTERVAL_S = 0.5


class ProcessManager:
    def __init__(self, script_path: Path | list, relay: SessionRelay, log_channel: OutputChannel) -> None:
        self._script = script_path
        self._relay = relay
        self._log = log_channel

    def run_session(self, date: datetime.date) -> None:
        """Kill any running signal_smt.py, then spawn fresh; relay output; restart once on unexpected exit; stop at 13:35 ET."""
        _kill_existing_signal_smt(self._script, self._log)
        restarted = False
        proc = None
        try:
            while True:
                proc = self._spawn()
                self._log.writeln(f"[ORCH] signal_smt.py started (pid={proc.pid})")
                exit_reason = self._monitor(proc)
                if exit_reason == "scheduled_stop":
                    self._log.writeln("[ORCH] Session ended — sending terminate signal")
                    self._terminate(proc)
                    return
                # Unexpected exit
                if not restarted:
                    self._log.writeln(
                        f"[ORCH] *** signal_smt.py exited unexpectedly (code={proc.returncode}) — restarting once ***"
                    )
                    restarted = True
                else:
                    self._log.writeln(
                        f"[ORCH] *** signal_smt.py exited again (code={proc.returncode}) — NOT restarting; waiting for session end ***"
                    )
                    self._wait_until_grace_end()
                    return
        except KeyboardInterrupt:
            if proc is not None and proc.poll() is None:
                self._log.writeln("[ORCH] Interrupt received — terminating signal_smt.py")
                self._terminate(proc)
            raise

    def _spawn(self) -> subprocess.Popen:
        if isinstance(self._script, list):
            cmd = self._script
        else:
            cmd = [sys.executable, str(self._script)]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )

    def _monitor(self, proc: subprocess.Popen) -> str:
        """Read stdout in a thread; poll for exit or 13:35 ET in main thread.

        Returns "scheduled_stop" or "unexpected_exit".
        """
        reader = threading.Thread(target=self._read_stdout, args=(proc,), daemon=True)
        reader.start()
        while True:
            if proc.poll() is not None:
                # Close stdout to unblock any still-reading thread before joining.
                if hasattr(proc.stdout, "close"):
                    proc.stdout.close()
                reader.join(timeout=2)
                return "unexpected_exit"
            now = datetime.datetime.now(tz=_ET)
            if now.time() >= _SESSION_GRACE_END:
                return "scheduled_stop"
            time.sleep(_POLL_INTERVAL_S)

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self._relay.emit(line.rstrip("\n"))

    def _terminate(self, proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=_SIGTERM_WAIT_S)
        except subprocess.TimeoutExpired:
            self._log.writeln("[ORCH] SIGTERM timeout — killing process")
            proc.kill()

    def _wait_until_grace_end(self) -> None:
        while datetime.datetime.now(tz=_ET).time() < _SESSION_GRACE_END:
            time.sleep(30)


def _kill_existing_signal_smt(script_path: Path | list, log: OutputChannel) -> None:
    """Terminate any running process whose cmdline contains signal_smt.py."""
    if isinstance(script_path, list):
        script_name = script_path[-1]
    else:
        script_name = script_path.name
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any(script_name in arg for arg in cmdline):
                log.writeln(f"[ORCH] Killing existing signal_smt.py (pid={proc.pid})")
                proc.terminate()
                proc.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass
