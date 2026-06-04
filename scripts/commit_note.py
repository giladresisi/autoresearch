# scripts/commit_note.py
# Records the running git commit into a live session's comments.md at orchestrator
# startup, so post-session analysis can tell which code version produced the session.
#
# Why: sessions now live in the machine-global sessions dir (paths.sessions_dir()) and
# are analyzed later, possibly after the worktree has moved on. A one-line commit note
# pins the session to an exact commit + subject + dirty flag. The writer itself is silent
# (no stdout) and never raises — startup must not be blocked by a git or filesystem hiccup.

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(args: list[str]) -> str | None:
    """Run a git command, returning stripped stdout, or None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def write_commit_note(
    comments_path,
    *,
    head: str | None = None,
    subject: str | None = None,
    dirty: bool | None = None,
) -> None:
    """Append a running-commit note line to a session's comments.md.

    Line format: `- Running commit: <short-sha> "<subject>" (dirty)`
    (the `(dirty)` suffix is added only when the working tree is dirty).

    Parent dirs and the file are created if missing; the note is APPENDED, never
    overwriting existing content. If head/subject/dirty are None they are derived
    from git best-effort (`git rev-parse --short HEAD`, `git log -1 --format=%s`,
    `git status --porcelain`). This function is silent and never raises.
    """
    try:
        if head is None:
            head = _git(["rev-parse", "--short", "HEAD"]) or "unknown"
        if subject is None:
            subject = _git(["log", "-1", "--format=%s"]) or ""
        if dirty is None:
            status = _git(["status", "--porcelain"])
            dirty = bool(status) if status is not None else False

        line = f'- Running commit: {head} "{subject}"'
        if dirty:
            line += " (dirty)"
        line += "\n"

        path = Path(comments_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        # Startup must never be blocked by a note-writing failure.
        pass
