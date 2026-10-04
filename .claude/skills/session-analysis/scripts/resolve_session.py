#!/usr/bin/env python
"""Session resolver + already-analyzed gate for the session-analysis skill.

Picks the session to analyze and decides whether the analysis may run:

- `--date YYYY-MM-DD` given -> that session.
- no date -> the LATEST session folder that holds a live run and is NOT the live run
  in progress. A live run is in progress when the orchestrator process is alive and the
  session window is open (the live-comment gate); its folder is skipped, so a call made
  mid-session resolves to the previous live run.
- the chosen session already has `session-analysis.md` and `--reanalyze` was not passed
  -> `STATUS: ALREADY_ANALYZED`, exit 3 (the skill stops without touching anything).
- the session's analysis worktree (`analyze-<mon>-<day>` beside this worktree, e.g.
  `analyze-oct-2`) already exists and `--reanalyze` was not passed
  -> `STATUS: WORKTREE_EXISTS`, exit 4 (same: the skill stops).

Prints `STATUS: ANALYZE` (exit 0), `STATUS: ALREADY_ANALYZED` (exit 3),
`STATUS: WORKTREE_EXISTS` (exit 4) or `STATUS: NO_SESSION` (exit 2), plus SESSION_DATE /
SESSION_FOLDER / SELECTED_BY / WORKTREE / WORKTREE_BRANCH / WORKTREE_EXISTS.

Run with the project venv from the worktree root:
    uv run python .claude/skills/session-analysis/scripts/resolve_session.py [--date D] [--reanalyze]
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# scripts -> session-analysis -> skills -> .claude -> <project root>
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
# The liveness check is the live-comment skill's; one definition of "a live run is on".
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "live-comment" / "scripts"))

import paths  # noqa: E402
from check_live_session import _orchestrator_alive, _window_open  # noqa: E402
from session_times import session_date_str  # noqa: E402

_ET = ZoneInfo("America/New_York")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# automation.main writes both from its first minute; a folder with neither never ran live.
_LIVE_RUN_MARKERS = ("signals.log", "events.jsonl")
_ANALYSIS_MARKER = "session-analysis.md"
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _worktree_name(date: str) -> str:
    """2026-10-02 -> analyze-oct-2 (also the branch name)."""
    d = datetime.strptime(date, "%Y-%m-%d")
    return f"analyze-{_MONTHS[d.month - 1]}-{d.day}"


def _running_session_date() -> tuple[str | None, str]:
    alive, detail = _orchestrator_alive()
    if not alive:
        return None, detail
    if not _window_open(datetime.now(tz=_ET)):
        return None, f"{detail}; session window closed (maintenance break)"
    return session_date_str(), detail


def _ran_live(folder: Path) -> bool:
    return any((folder / m).exists() for m in _LIVE_RUN_MARKERS)


def _latest_finished_session(sessions: Path, running: str | None) -> str | None:
    dates = sorted(
        (p.name for p in sessions.iterdir() if p.is_dir() and _DATE_RE.match(p.name)),
        reverse=True,
    )
    for d in dates:
        if d != running and _ran_live(sessions / d):
            return d
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="session date YYYY-MM-DD (default: latest finished live run)")
    ap.add_argument("--reanalyze", action="store_true",
                    help="analyze even if the session already has session-analysis.md")
    args = ap.parse_args()

    sessions = paths.sessions_dir()
    running, live_detail = _running_session_date()
    print(f"LIVE_NOW: {running or 'none'} ({live_detail})")

    if args.date:
        if not _DATE_RE.match(args.date):
            print("STATUS: NO_SESSION")
            print(f"REASON: --date must be YYYY-MM-DD, got {args.date!r}")
            sys.exit(2)
        date, selected_by = args.date, "argument"
    else:
        date = _latest_finished_session(sessions, running) if sessions.is_dir() else None
        selected_by = "default (latest live run that is no longer running)"
        if date is None:
            print("STATUS: NO_SESSION")
            print(f"REASON: no finished live run found under {sessions.as_posix()}")
            sys.exit(2)

    folder = sessions / date
    if not folder.is_dir():
        print("STATUS: NO_SESSION")
        print(f"REASON: no session directory found for {date}")
        sys.exit(2)

    print(f"SESSION_DATE: {date}")
    print(f"SESSION_FOLDER: {folder.as_posix()}")
    print(f"SELECTED_BY: {selected_by}")
    if date == running:
        print("WARNING: this session's live run is still in progress")

    # Worktrees are siblings: the analysis worktree sits beside the one this runs in.
    worktree = ROOT.parent / _worktree_name(date)
    print(f"WORKTREE: {worktree.as_posix()}")
    print(f"WORKTREE_BRANCH: {worktree.name}")
    print(f"WORKTREE_EXISTS: {str(worktree.exists()).lower()}")

    marker = folder / _ANALYSIS_MARKER
    if marker.exists() and not args.reanalyze:
        analyzed_ts = marker.stat().st_mtime
        analyzed_at = datetime.fromtimestamp(analyzed_ts, tz=_ET)
        # The live run was resumed into the same folder after the analysis was written.
        stale = any(
            (folder / m).exists() and (folder / m).stat().st_mtime > analyzed_ts
            for m in _LIVE_RUN_MARKERS
        )
        print("STATUS: ALREADY_ANALYZED")
        print(f"ANALYZED_AT: {analyzed_at.strftime('%Y-%m-%d %H:%M')} ET")
        print(f"ANALYSIS_FILE: {marker.as_posix()}")
        print(f"ANALYSIS_STALE: {str(stale).lower()}")
        sys.exit(3)

    if worktree.exists() and not args.reanalyze:
        print("STATUS: WORKTREE_EXISTS")
        sys.exit(4)

    print("STATUS: ANALYZE")
    print(f"REANALYSIS: {str(marker.exists()).lower()}")


if __name__ == "__main__":
    main()
