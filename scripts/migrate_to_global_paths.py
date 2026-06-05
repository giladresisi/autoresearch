#!/usr/bin/env python
# scripts/migrate_to_global_paths.py
#
# WHAT: One-time, idempotent migration of existing in-project runtime data into the new
# global / worktree-local locations defined by paths.py.
#   - Parquets  : COPY  data/*.parquet (+ their *.parquet.bak siblings) into BOTH
#                 paths.data_main_dir() AND paths.data_live_dir() (seed both sides).
#   - Sessions  : MOVE  sessions/<date>/ subdirs into paths.sessions_dir().
#   - Regression: MOVE  data/regression/<date>/ folders into paths.regression_dir(),
#                 preserving the date-folder structure.
#
# WHY: The path restructure (.agents/plans/global-path-restructure.md) relocates production
# parquets and live sessions into a machine-global folder and regression outputs into a
# worktree-root dir so parallel-worktree backtests no longer collide with the live
# orchestrator (Windows [WinError 5] rename-over-open). This script seeds those new
# locations from whatever already exists in this worktree, ONCE.
#
# This is a one-off operator CLI, so printing a human-readable summary to stdout is allowed
# (the silent-production rule applies to the trading hot path, not migration tooling).
#
# Design: all real-FS side effects live behind plan_migration() / migrate() so tests can
# drive them against a temp tree using ACT_GLOBAL_DIR / ACT_REGRESSION_DIR env overrides.

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Import the path source of truth. Works whether invoked as `python scripts/migrate_...py`
# (repo root on cwd) or `python -m scripts.migrate_...` (repo root on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths  # noqa: E402


# ---------------------------------------------------------------------------
# Action model
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """A single planned filesystem operation.

    kind     : "copy" (parquets) or "move" (sessions / regression date folders)
    src      : source path
    dst      : destination path
    category : "parquet" | "session" | "regression" (for the summary)
    skip     : non-None reason string => this action will NOT run (already-done /
               refused). Used for idempotency and refuse-overwrite reporting.
    """
    kind: str
    src: Path
    dst: Path
    category: str
    skip: str | None = None


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    # Categories that had no source at all -> reported as "nothing to migrate".
    empty_categories: list[str] = field(default_factory=list)

    def runnable(self) -> list[Action]:
        return [a for a in self.actions if a.skip is None]

    def skipped(self) -> list[Action]:
        return [a for a in self.actions if a.skip is not None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_nonempty_dir(p: Path) -> bool:
    return p.exists() and p.is_dir() and any(p.iterdir())


def _files_equal(a: Path, b: Path) -> bool:
    """Cheap sameness check (size) so a re-run of the parquet copy is a no-op rather than a
    refused overwrite. Full byte-compare is overkill for an idempotency guard."""
    try:
        return a.stat().st_size == b.stat().st_size
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Planning (pure: inspects the FS, mutates nothing)
# ---------------------------------------------------------------------------

def plan_migration(project_root: Path, force: bool = False) -> Plan:
    """Inspect the worktree and the (env-resolved) global / regression targets and build the
    list of Actions. Pure read-only: never writes. `force` only affects whether an existing
    NON-EMPTY / differing target is marked skip="refused" vs allowed to overwrite/merge."""
    plan = Plan()

    main_dir = paths.data_main_dir()
    live_dir = paths.data_live_dir()
    sessions_target = paths.sessions_dir()
    regression_target = paths.regression_dir()

    # --- Parquets: COPY data/*.parquet (+ *.parquet.bak) into BOTH main and live ---
    data_dir = project_root / "data"
    parquet_srcs: list[Path] = []
    if data_dir.is_dir():
        parquet_srcs = sorted(data_dir.glob("*.parquet")) + sorted(data_dir.glob("*.parquet.bak"))
    if not parquet_srcs:
        plan.empty_categories.append("parquet")
    for src in parquet_srcs:
        for dst_dir in (main_dir, live_dir):
            dst = dst_dir / src.name
            skip = None
            if dst.exists():
                if _files_equal(src, dst):
                    skip = "already present (same size)"
                elif not force:
                    skip = "refused: target exists (use --force)"
            plan.actions.append(Action("copy", src, dst, "parquet", skip))

    # --- Sessions: MOVE sessions/<date>/ -> <global>/sessions/<date> ---
    sessions_src_root = project_root / "sessions"
    session_dirs = (
        sorted(p for p in sessions_src_root.iterdir() if p.is_dir())
        if sessions_src_root.is_dir() else []
    )
    if not session_dirs:
        plan.empty_categories.append("session")
    for src in session_dirs:
        dst = sessions_target / src.name
        # A non-empty existing target is a genuine collision -> refuse unless --force.
        # An empty existing target (e.g. a stub the getters created) is harmless: merge into it.
        skip = None
        if _is_nonempty_dir(dst) and not force:
            skip = "refused: target exists and is non-empty (use --force)"
        plan.actions.append(Action("move", src, dst, "session", skip))

    # --- Regression: MOVE data/regression/<date>/ -> <regression>/<date> ---
    regression_src_root = project_root / "data" / "regression"
    regression_dirs: list[Path] = []
    if regression_src_root.is_dir():
        for p in sorted(regression_src_root.iterdir()):
            if p.is_dir():
                regression_dirs.append(p)
    if not regression_dirs:
        plan.empty_categories.append("regression")
    for src in regression_dirs:
        dst = regression_target / src.name
        skip = None
        if _is_nonempty_dir(dst) and not force:
            skip = "refused: target exists and is non-empty (use --force)"
        plan.actions.append(Action("move", src, dst, "regression", skip))

    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _do_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _do_move(src: Path, dst: Path, force: bool) -> None:
    """Move a directory tree src -> dst. If dst exists and force is set, merge src's contents
    into dst (overwriting files) then remove the now-empty src."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.move(str(src), str(dst))
        return
    # dst exists (force path): merge file-by-file, then drop the source.
    for child in src.rglob("*"):
        rel = child.relative_to(src)
        target = dst / rel
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)
    shutil.rmtree(src)


def migrate(project_root: Path, dry_run: bool = False, force: bool = False) -> Plan:
    """Build the plan and (unless dry_run) execute its runnable actions. Returns the Plan so
    callers/tests can inspect what was planned, run, and skipped. Ensures the new target dirs
    exist via the paths.py getters (which mkdir) regardless of whether anything is migrated."""
    # Materialize targets up front (paths getters mkdir parents).
    paths.data_live_dir()
    paths.data_main_dir()
    paths.sessions_dir()
    paths.regression_dir()

    plan = plan_migration(project_root, force=force)

    if dry_run:
        return plan

    for action in plan.runnable():
        if action.kind == "copy":
            _do_copy(action.src, action.dst)
        elif action.kind == "move":
            _do_move(action.src, action.dst, force=force)

    return plan


# ---------------------------------------------------------------------------
# CLI / summary
# ---------------------------------------------------------------------------

def _print_summary(plan: Plan, dry_run: bool) -> None:
    prefix = "[DRY-RUN] would " if dry_run else ""
    runnable = plan.runnable()
    skipped = plan.skipped()

    print("=" * 70)
    print("migrate_to_global_paths" + (" (dry-run)" if dry_run else ""))
    print("=" * 70)
    print(f"global root      : {paths.global_root()}")
    print(f"  data/main      : {paths.data_main_dir()}")
    print(f"  data/live      : {paths.data_live_dir()}")
    print(f"  sessions       : {paths.sessions_dir()}")
    print(f"regression dir   : {paths.regression_dir()}")
    print("-" * 70)

    for category, verb in (("parquet", "COPY"), ("session", "MOVE"), ("regression", "MOVE")):
        cat_runnable = [a for a in runnable if a.category == category]
        cat_skipped = [a for a in skipped if a.category == category]
        if category in plan.empty_categories:
            print(f"{category:11}: nothing to migrate")
            continue
        if not cat_runnable and not cat_skipped:
            print(f"{category:11}: nothing to migrate")
            continue
        print(f"{category}:")
        for a in cat_runnable:
            print(f"  {prefix}{verb} {a.src}  ->  {a.dst}")
        for a in cat_skipped:
            print(f"  SKIP ({a.skip}) {a.src}  ->  {a.dst}")

    print("-" * 70)
    print(f"planned actions  : {len(runnable)}   skipped: {len(skipped)}")
    if not runnable and not skipped:
        print("nothing to migrate.")
    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-time migration of in-project data/sessions/regression into the "
                    "global + worktree-local locations defined by paths.py."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the planned actions and change nothing.")
    parser.add_argument("--force", action="store_true",
                        help="Allow overwriting / merging into non-empty existing targets.")
    parser.add_argument("--project-root", default=None,
                        help="Worktree root to migrate FROM (default: current directory).")
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()

    plan = migrate(project_root, dry_run=args.dry_run, force=args.force)
    _print_summary(plan, dry_run=args.dry_run)

    # A dry-run is purely informational -> always exit 0 (even if a real run WOULD be blocked).
    # On a real run, exit non-zero if something was refused so callers detect a blocked migration.
    refused = [a for a in plan.skipped() if a.skip and a.skip.startswith("refused")]
    if refused and not args.dry_run:
        print(f"\nWARNING: {len(refused)} action(s) refused (non-empty target). "
              f"Re-run with --force to overwrite/merge.", file=sys.stderr)
        return 3
    if refused:
        print(f"\nNOTE (dry-run): {len(refused)} action(s) would be refused (non-empty "
              f"target); a real run would need --force to overwrite/merge.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
