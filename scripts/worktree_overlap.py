"""Which OTHER worktrees of this repo are touching the files you are about to edit?

    uv run python scripts/worktree_overlap.py agent/trader/executor.py l2-mechanisms.md
    uv run python scripts/worktree_overlap.py --all          # every other worktree's dirty / ahead files

For each other worktree: its branch, the files it has modified but not committed, and
the files its commits since `origin/master` touch. With paths given, prints only the
worktrees whose files intersect them and exits 3 when there is any overlap, so a skill
step can stop and ask before editing. Read-only; never touches another worktree.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.study import corpus  # noqa: E402


def _git(root: Path, *args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=True).stdout
    except Exception:
        return ""


def _norm(p: str) -> str:
    return p.strip().replace("\\", "/").strip("/")


def inspect(root: Path) -> dict:
    """{branch, dirty: [paths], ahead: [paths], ahead_commits: int} for one worktree."""
    branch = _git(root, "branch", "--show-current").strip() or "(detached)"
    dirty = []
    for line in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines():
        if len(line) > 3:
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ")[-1]
            dirty.append(_norm(path))
    ahead_files = _git(root, "diff", "--name-only", "origin/master...HEAD").splitlines()
    ahead_n = _git(root, "rev-list", "--count", "origin/master..HEAD").strip()
    return {"root": str(root), "branch": branch, "dirty": sorted(set(dirty)),
            "ahead": sorted({_norm(p) for p in ahead_files if p.strip()}),
            "ahead_commits": int(ahead_n) if ahead_n.isdigit() else 0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*", help="repo-relative files you intend to edit")
    ap.add_argument("--all", action="store_true", help="list every other worktree's files")
    args = ap.parse_args()
    if not args.paths and not args.all:
        ap.error("give paths, or --all")
    here = Path(_git(corpus.repo_root(), "rev-parse", "--show-toplevel").strip() or corpus.repo_root())
    want = {_norm(p) for p in args.paths}
    overlap_found = False
    for root in corpus.worktrees():
        if Path(root).resolve() == here.resolve():
            continue
        info = inspect(Path(root))
        touched = set(info["dirty"]) | set(info["ahead"])
        hits = sorted(touched & want) if want else sorted(touched)
        if not hits:
            continue
        overlap_found = overlap_found or bool(want)
        print(f"{info['root']}  [{info['branch']}; {info['ahead_commits']} commit(s) ahead of origin/master]")
        for p in hits:
            tags = [t for t, s in (("uncommitted", info["dirty"]), ("ahead-of-master", info["ahead"])) if p in s]
            print(f"   {p}  ({', '.join(tags)})")
    if want and not overlap_found:
        print("no other worktree touches:", ", ".join(sorted(want)))
    return 3 if overlap_found else 0


if __name__ == "__main__":
    raise SystemExit(main())
