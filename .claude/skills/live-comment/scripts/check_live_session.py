#!/usr/bin/env python
"""Gate + locator for the live-comment skill.

Prints `STATUS: LIVE` plus the current session's comments.md path ONLY when an
orchestrator process is alive AND the trading session window is currently open.
Otherwise prints `STATUS: NOT_LIVE` with a reason and exits 2 — so the skill can
bail out early without creating or touching any files.

Run with the project venv from anywhere:
    uv run python .claude/skills/live-comment/scripts/check_live_session.py
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# scripts -> live-comment -> skills -> .claude -> <project root>
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from session_times import SESSION_OPEN, SESSION_CLOSE, session_date_str  # noqa: E402

_ET = ZoneInfo("America/New_York")


def _fail(reason: str) -> None:
    print("STATUS: NOT_LIVE")
    print(f"REASON: {reason}")
    sys.exit(2)


def _orchestrator_alive() -> tuple[bool, str]:
    """Best-effort liveness check for the orchestrator process.

    We anchor on orchestrator.pid (written by trade.py at startup) and confirm the
    PID is running. psutil is the reliable cross-platform check on Windows, where
    os.kill(pid, 0) misbehaves. We also sanity-check the process name to guard
    against PID reuse by an unrelated program after the orchestrator exits.
    """
    import paths  # ROOT is on sys.path
    pid_file = paths.general_live_dir() / "orchestrator.pid"
    if not pid_file.exists():
        return False, "no orchestrator.pid file (orchestrator not started)"
    raw = pid_file.read_text().strip()
    if not raw:
        return False, "orchestrator.pid is empty"
    try:
        pid = int(raw)
    except ValueError:
        return False, f"orchestrator.pid is not a number: {raw!r}"
    try:
        import psutil
    except ImportError:
        # Can't verify — assume alive if the pid file is present (best effort).
        return True, f"orchestrator pid {pid} (liveness unverified — psutil missing)"
    if not psutil.pid_exists(pid):
        return False, f"orchestrator pid {pid} is not running"
    try:
        name = psutil.Process(pid).name().lower()
    except psutil.Error:
        name = ""
    if name and "python" not in name and "uv" not in name:
        return False, f"pid {pid} is alive but is '{name}', not the orchestrator (PID reuse?)"
    return True, f"orchestrator pid {pid} alive"


def _window_open(now_et: datetime) -> bool:
    """Open 18:05 ET → 16:55 ET next day; closed 16:55–18:05 (maintenance gap)."""
    t = now_et.time()
    return t >= SESSION_OPEN or t < SESSION_CLOSE


def main() -> None:
    alive, detail = _orchestrator_alive()
    if not alive:
        _fail(detail)

    now_et = datetime.now(tz=_ET)
    if not _window_open(now_et):
        _fail(
            f"session window closed (maintenance {SESSION_CLOSE.strftime('%H:%M')}-"
            f"{SESSION_OPEN.strftime('%H:%M')} ET); now {now_et.strftime('%H:%M')} ET"
        )

    import paths  # ROOT is on sys.path
    session_date = session_date_str()
    folder = paths.sessions_dir() / session_date
    comments = folder / "comments.md"

    print("STATUS: LIVE")
    print(f"DETAIL: {detail}")
    print(f"SESSION_DATE: {session_date}")
    print(f"SESSION_FOLDER: {folder.as_posix()}")
    print(f"COMMENTS_PATH: {comments.as_posix()}")
    print(f"COMMENTS_EXISTS: {str(comments.exists()).lower()}")
    print(f"NOW_ET: {now_et.strftime('%Y-%m-%d %H:%M')} ET")


if __name__ == "__main__":
    main()
