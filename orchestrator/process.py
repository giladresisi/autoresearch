# orchestrator/process.py
# Manages the signal_smt.py subprocess lifecycle: spawn, stdout relay, restart-on-crash, and scheduled stop.
import datetime
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil

from orchestrator.output import OutputChannel
from orchestrator.relay import SessionRelay

from session_times import SESSION_CLOSE as _SESSION_GRACE_END

_ET = ZoneInfo("America/New_York")
_SIGTERM_WAIT_S = 10
_POLL_INTERVAL_S = 0.5


class ProcessManager:
    def __init__(self, script_path: Path | list, relay: SessionRelay, log_channel: OutputChannel, extra_env: dict | None = None) -> None:
        self._script = script_path
        self._relay = relay
        self._log = log_channel
        self._extra_env = extra_env

    def _process_name(self) -> str:
        if isinstance(self._script, list):
            return self._script[-1]
        return self._script.name

    def run_session(self, date: datetime.date, grace_end_dt: datetime.datetime | None = None) -> str | None:
        """Kill any stale instance of this subprocess, spawn fresh, relay output, restart once on unexpected exit.

        grace_end_dt: datetime when the session ends (SESSION_CLOSE the *next* calendar day when
        the session opened after 18:00 ET today).  If None, falls back to time-only comparison
        against SESSION_CLOSE — which is wrong for evening sessions but kept for safety.

        Returns "ib_disconnected" if automation exited with code 2; None otherwise.
        """
        name = self._process_name()
        _kill_existing_signal_smt(self._script, self._log)
        restarted = False
        proc = None
        try:
            while True:
                proc = self._spawn()
                self._log.writeln(f"[ORCH] {name} started (pid={proc.pid})")
                exit_reason = self._monitor(proc, grace_end_dt=grace_end_dt)
                if exit_reason == "scheduled_stop":
                    self._log.writeln("[ORCH] Session ended — sending terminate signal")
                    self._terminate(proc)
                    return None
                if exit_reason == "ib_disconnected":
                    self._log.writeln(
                        f"[ORCH] *** IB Gateway disconnected (exit code 2) — not restarting ***"
                    )
                    return "ib_disconnected"
                # Unexpected exit
                if not restarted:
                    self._log.writeln(
                        f"[ORCH] *** {name} exited unexpectedly (code={proc.returncode}) — restarting once ***"
                    )
                    restarted = True
                else:
                    self._log.writeln(
                        f"[ORCH] *** {name} exited again (code={proc.returncode}) — NOT restarting; waiting for session end ***"
                    )
                    self._wait_until_grace_end(grace_end_dt=grace_end_dt)
                    return None
        except KeyboardInterrupt:
            if proc is not None and proc.poll() is None:
                self._log.writeln(f"[ORCH] Interrupt received — terminating {name}")
                self._terminate(proc)
            raise

    def _spawn(self) -> subprocess.Popen:
        if isinstance(self._script, list):
            cmd = self._script
        else:
            cmd = [sys.executable, str(self._script)]
        _env = {**os.environ, **self._extra_env} if self._extra_env else None
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env,
        )

    def _monitor(self, proc: subprocess.Popen, grace_end_dt: datetime.datetime | None = None) -> str:
        """Read stdout in a thread; poll for exit or session end in main thread.

        Returns "scheduled_stop", "ib_disconnected" (exit code 2), or "unexpected_exit".
        """
        reader = threading.Thread(target=self._read_stdout, args=(proc,), daemon=True)
        reader.start()
        while True:
            if proc.poll() is not None:
                # Close stdout to unblock any still-reading thread before joining.
                if hasattr(proc.stdout, "close"):
                    proc.stdout.close()
                reader.join(timeout=2)
                if proc.returncode == 2:
                    return "ib_disconnected"
                return "unexpected_exit"
            now = datetime.datetime.now(tz=_ET)
            if grace_end_dt is not None:
                if now >= grace_end_dt:
                    return "scheduled_stop"
            elif now.time() >= _SESSION_GRACE_END:
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

    def _wait_until_grace_end(self, grace_end_dt: datetime.datetime | None = None) -> None:
        while True:
            now = datetime.datetime.now(tz=_ET)
            if grace_end_dt is not None:
                if now >= grace_end_dt:
                    break
            elif now.time() >= _SESSION_GRACE_END:
                break
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
                log.writeln(f"[ORCH] Killing existing {script_name} (pid={proc.pid})")
                proc.terminate()
                proc.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass
