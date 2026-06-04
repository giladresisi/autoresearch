# tests/test_commit_note.py
# Tests for scripts/commit_note.write_commit_note — the startup commit-note writer.
from __future__ import annotations

from pathlib import Path

from scripts.commit_note import write_commit_note


def test_explicit_args_writes_expected_line(tmp_path):
    comments = tmp_path / "2026-06-04" / "comments.md"
    write_commit_note(comments, head="abc1234", subject="fix: thing", dirty=True)

    content = comments.read_text(encoding="utf-8")
    assert content == '- Running commit: abc1234 "fix: thing" (dirty)\n'


def test_clean_tree_omits_dirty_suffix(tmp_path):
    comments = tmp_path / "comments.md"
    write_commit_note(comments, head="def5678", subject="feat: clean", dirty=False)

    assert comments.read_text(encoding="utf-8") == '- Running commit: def5678 "feat: clean"\n'


def test_appends_does_not_overwrite(tmp_path):
    comments = tmp_path / "comments.md"
    comments.write_text("# Session notes\n\nExisting observation.\n", encoding="utf-8")

    write_commit_note(comments, head="abc1234", subject="fix: thing", dirty=False)

    content = comments.read_text(encoding="utf-8")
    assert content.startswith("# Session notes\n\nExisting observation.\n")
    assert content.endswith('- Running commit: abc1234 "fix: thing"\n')


def test_creates_file_and_parents_when_missing(tmp_path):
    comments = tmp_path / "global" / "sessions" / "2026-06-04" / "comments.md"
    assert not comments.exists()

    write_commit_note(comments, head="abc1234", subject="init", dirty=False)

    assert comments.exists()
    assert comments.parent.is_dir()
    assert comments.read_text(encoding="utf-8") == '- Running commit: abc1234 "init"\n'
