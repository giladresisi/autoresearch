# tests/test_migrate_to_global_paths.py
#
# WHAT: Unit tests for scripts/migrate_to_global_paths.py — the one-time migration of
# in-project data/sessions/regression into the global + worktree-local locations.
# WHY: The migration touches real files (copy parquets to both main+live, move sessions and
# regression date folders). These tests drive it entirely on a TEMP tree by overriding
# ACT_GLOBAL_DIR / ACT_REGRESSION_DIR and chdir-ing into a tmp project root, so nothing real
# is moved. Covers: happy path, dry-run no-op, refuse-overwrite (with/without --force) and
# idempotency / empty-tree.

import importlib

import pytest

import paths
from scripts import migrate_to_global_paths as mig


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Build an isolated project worktree + global + regression layout under tmp_path and
    point paths.py at it via env overrides. Returns a small namespace of the key dirs."""
    project = tmp_path / "worktree"
    global_dir = tmp_path / "global"
    regression_dir = tmp_path / "regression_out"
    project.mkdir()

    monkeypatch.setenv("ACT_GLOBAL_DIR", str(global_dir))
    monkeypatch.setenv("ACT_REGRESSION_DIR", str(regression_dir))
    monkeypatch.chdir(project)
    # paths reads env at call time; reload to be safe against any cached state.
    importlib.reload(paths)

    return type("Tree", (), {
        "project": project,
        "global_dir": global_dir,
        "regression_dir": regression_dir,
    })


def _seed(project):
    """Seed a fake parquet, a session, and a regression date folder in the worktree."""
    data = project / "data"
    data.mkdir()
    (data / "MNQ_1m.parquet").write_bytes(b"PARQUET-BYTES-NOT-REAL")
    (data / "MNQ_1m.parquet.bak").write_bytes(b"PARQUET-BAK-BYTES")

    sess = project / "sessions" / "2026-06-01"
    sess.mkdir(parents=True)
    (sess / "events.jsonl").write_text('{"e": 1}\n', encoding="utf-8")

    reg = project / "data" / "regression" / "2026-06-02"
    reg.mkdir(parents=True)
    (reg / "trades.tsv").write_text("pnl\t1.0\n", encoding="utf-8")


def test_happy_path(tree):
    """Parquet COPIED into both main and live; session MOVED; regression date folder MOVED."""
    _seed(tree.project)

    plan = mig.migrate(tree.project, dry_run=False, force=False)

    # Parquet (+ .bak) copied into BOTH main and live; source still present (copy, not move).
    for name in ("MNQ_1m.parquet", "MNQ_1m.parquet.bak"):
        assert (paths.general_main_dir() / name).read_bytes() == (tree.project / "data" / name).read_bytes()
        assert (paths.general_live_dir() / name).read_bytes() == (tree.project / "data" / name).read_bytes()
        assert (tree.project / "data" / name).exists()  # COPY: source preserved

    # Session MOVED into global sessions (source gone, dest present with its file).
    moved_session = paths.sessions_dir() / "2026-06-01"
    assert (moved_session / "events.jsonl").read_text(encoding="utf-8") == '{"e": 1}\n'
    assert not (tree.project / "sessions" / "2026-06-01").exists()

    # Regression date folder MOVED into regression_dir/sessions/ (structure preserved, source gone).
    moved_reg = tree.regression_dir / "sessions" / "2026-06-02"
    assert (moved_reg / "trades.tsv").read_text(encoding="utf-8") == "pnl\t1.0\n"
    assert not (tree.project / "data" / "regression" / "2026-06-02").exists()

    assert plan.empty_categories == []


def test_dry_run_changes_nothing(tree):
    """dry_run=True must mutate nothing on disk."""
    _seed(tree.project)

    plan = mig.migrate(tree.project, dry_run=True, force=False)

    # Nothing copied into main/live.
    assert not (paths.general_main_dir() / "MNQ_1m.parquet").exists()
    assert not (paths.general_live_dir() / "MNQ_1m.parquet").exists()
    # Session NOT moved.
    assert (tree.project / "sessions" / "2026-06-01" / "events.jsonl").exists()
    assert not (paths.sessions_dir() / "2026-06-01").exists()
    # Regression NOT moved.
    assert (tree.project / "data" / "regression" / "2026-06-02" / "trades.tsv").exists()
    assert not (tree.regression_dir / "sessions" / "2026-06-02").exists()
    # But the plan still enumerated runnable actions.
    assert len(plan.runnable()) > 0


def test_refuse_overwrite_then_force(tree):
    """Refuse-overwrite asserted on the SESSION category (easiest: pre-create a non-empty
    target session). Without --force the existing file is preserved (refused). With --force
    the migration merges/overwrites and the source is removed."""
    _seed(tree.project)

    # Pre-create a NON-EMPTY target: global sessions already has 2026-06-01 with a file.
    existing = paths.sessions_dir() / "2026-06-01"
    existing.mkdir(parents=True)
    (existing / "preexisting.txt").write_text("KEEP", encoding="utf-8")

    # WITHOUT force: refuses -> the source session is NOT moved, target untouched.
    plan = mig.migrate(tree.project, dry_run=False, force=False)
    refused = [a for a in plan.skipped()
               if a.category == "session" and a.skip and a.skip.startswith("refused")]
    assert refused, "expected the non-empty session target to be refused"
    assert (tree.project / "sessions" / "2026-06-01" / "events.jsonl").exists()  # source kept
    assert (existing / "preexisting.txt").read_text(encoding="utf-8") == "KEEP"  # not clobbered
    assert not (existing / "events.jsonl").exists()  # source content not merged in

    # WITH force: proceeds -> source merged into target and removed.
    plan2 = mig.migrate(tree.project, dry_run=False, force=True)
    assert (existing / "events.jsonl").read_text(encoding="utf-8") == '{"e": 1}\n'
    assert (existing / "preexisting.txt").exists()  # merge keeps the pre-existing file too
    assert not (tree.project / "sessions" / "2026-06-01").exists()  # source removed
    assert not any(a.skip and a.skip.startswith("refused") for a in plan2.skipped())


def test_idempotent_and_empty(tree):
    """A worktree with NO parquets and NO regression must not raise and must report nothing
    to migrate for those categories. Running migrate twice must also be safe (idempotent)."""
    # Only a session, no data/ parquets, no data/regression.
    sess = tree.project / "sessions" / "2026-06-01"
    sess.mkdir(parents=True)
    (sess / "events.jsonl").write_text("x\n", encoding="utf-8")

    plan = mig.migrate(tree.project, dry_run=False, force=False)
    assert "parquet" in plan.empty_categories
    assert "regression" in plan.empty_categories

    # Second run: session already migrated -> skipped, no error, no duplication.
    plan2 = mig.migrate(tree.project, dry_run=False, force=False)
    assert (paths.sessions_dir() / "2026-06-01" / "events.jsonl").exists()
    # No runnable actions remain on the second pass (everything already done / empty).
    assert plan2.runnable() == []
