"""The thesis-cache key resolves its backend/model the way `make_backend` does — shell
first, then the worktree .env — so a warm replay hits a `--seed` recording (2026-09-26:
a warm replay keyed ":" and missed a recording made an hour earlier)."""
import os

import pytest

from agent import run_agent as ra
from agent.trader import cached_backend as cb

_VARS = ("ACT_TRADER_BACKEND", "ACT_TRADER_MODEL", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    """A fake worktree root with its own .env and a shell holding none of the vars."""
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ra, "REPO_ROOT", str(tmp_path))

    def _write(text):
        (tmp_path / ".env").write_text(text, encoding="utf-8")
    return _write


def test_backend_name_reads_the_worktree_env_when_the_shell_has_no_key(worktree):
    worktree("OPENROUTER_API_KEY=placeholder\n")
    assert cb.backend_name() == "openrouter"


def test_the_shell_wins_over_the_file(worktree, monkeypatch):
    worktree("OPENROUTER_API_KEY=placeholder\n")
    monkeypatch.setenv("ACT_TRADER_BACKEND", "anthropic")
    assert cb.backend_name() == "anthropic"


def test_an_empty_shell_value_wins_like_setdefault(worktree, monkeypatch):
    worktree("OPENROUTER_API_KEY=placeholder\nANTHROPIC_API_KEY=placeholder\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert cb.backend_name() == "anthropic"


def test_no_key_anywhere_is_still_blank(worktree):
    worktree("")
    assert cb.backend_name() == ""


def test_resolving_never_mutates_the_environment(worktree):
    worktree("OPENROUTER_API_KEY=placeholder\nACT_TRADER_MODEL=some/model\n")
    cb.prompt_parts()
    assert not any(v in os.environ for v in _VARS)


def test_the_warm_key_equals_the_key_after_make_backend_loaded_the_env(worktree):
    """The defect itself: the model id must not depend on whether `.env` was loaded."""
    worktree("OPENROUTER_API_KEY=placeholder\nACT_TRADER_MODEL=some/model\n")
    warm = cb.prompt_parts()[3]
    ra.load_env_file(os.path.join(ra.REPO_ROOT, ".env"))     # what --seed's make_backend does
    try:
        assert cb.prompt_parts()[3] == warm == "openrouter:some/model"
    finally:
        for v in _VARS:
            os.environ.pop(v, None)


def test_read_env_file_matches_load_env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('# c\nexport A_X="1"\nB_X = two\n\nbad line\n', encoding="utf-8")
    assert ra.read_env_file(str(p)) == {"A_X": "1", "B_X": "two"}
    monkeypatch.delenv("A_X", raising=False)
    monkeypatch.setenv("B_X", "shell")
    ra.load_env_file(str(p))
    assert os.environ["A_X"] == "1" and os.environ["B_X"] == "shell"
    monkeypatch.delenv("A_X")
    assert ra.read_env_file(str(tmp_path / "missing")) == {}
