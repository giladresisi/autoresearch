"""The single place that knows which OS the live process runs on (plan 52).

Import-cheap on purpose (no psutil): trade.py and orchestrator/main.py import it at
module level.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_CREATE_NO_WINDOW = 0x08000000
_PY_NAME = re.compile(r"python(3(\.\d+)*)?")


def is_python_process_name(name) -> bool:
    """True for a Python interpreter's process name on Windows/macOS/Linux.

    macOS venv interpreters show up as `python`, `Python` or `python3.12`; Windows as
    `python.exe` / `pythonw.exe`. Not `uv`, `powershell`, `pythonista`, empty or None.
    The cmdline substring and worktree-cwd checks remain the real discriminators.
    """
    if not name:
        return False
    n = str(name).lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n == "pythonw" or _PY_NAME.fullmatch(n) is not None


def detached_popen_kwargs() -> dict:
    """Popen kwargs that launch the orchestrator detached from the terminal.

    Windows: no console window (today's value). POSIX: a new session, so closing the
    terminal does not SIGHUP it; `creationflags` must NOT be passed there (ValueError).
    """
    if os.name == "nt":
        return {"creationflags": _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def prevent_idle_sleep(pid: int | None = None):
    """Keep the machine awake while this process runs. Never raises.

    Idle sleep / Modern Standby tears down the IB TCP session mid-session. Windows:
    SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED), returns None. macOS:
    spawns `caffeinate -dimsu -w <pid>` (ends by itself when pid exits) and returns the
    Popen. Other POSIX: no-op.
    """
    try:
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
            except Exception:
                pass
            return None
        if sys.platform == "darwin":
            target = os.getpid() if pid is None else pid
            try:
                return subprocess.Popen(
                    ["caffeinate", "-dimsu", "-w", str(target)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                print("macOS sleep prevention unavailable", file=sys.stderr)
                return None
    except Exception:
        pass
    return None
