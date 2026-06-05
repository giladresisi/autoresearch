# tests/test_orchestrator_kill_scope.py
# Worktree-scoping for the process-kill scans: a sibling worktree's (possibly LIVE) orchestrator
# / automation.main must NEVER be terminated. Only processes whose cwd is THIS worktree's root
# are killed; processes in other worktrees, or whose cwd is unreadable, are left alone.

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _fake_proc(pid, cwd, *, name="python.exe", cmdline=("python", "-m", "orchestrator.main"),
               cwd_raises=False):
    p = MagicMock()
    p.pid = pid
    p.info = {"pid": pid, "name": name, "cmdline": list(cmdline)}
    if cwd_raises:
        p.cwd.side_effect = OSError("cwd unreadable")
    else:
        p.cwd.return_value = cwd
    return p


# ---------------------------------------------------------------------------
# orchestrator.main._kill_stale_orchestrator
# ---------------------------------------------------------------------------

def test_kill_stale_orchestrator_scopes_to_this_worktree(tmp_path, monkeypatch):
    import orchestrator.main as om
    import paths

    root = str(Path(om.__file__).resolve().parent.parent)  # this worktree's root
    same       = _fake_proc(991001, cwd=root)                               # → terminate
    other      = _fake_proc(991002, cwd=str(tmp_path / "other_worktree"))   # different worktree → skip
    unreadable = _fake_proc(991003, cwd=None, cwd_raises=True)              # cwd unreadable → skip

    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [same, other, unreadable])
    # PID file now resolves under paths.general_live_dir(); isolate the global root to tmp.
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))

    om._kill_stale_orchestrator()

    same.terminate.assert_called_once()
    other.terminate.assert_not_called()
    unreadable.terminate.assert_not_called()
    # PID file written to the general live folder (own pid recorded)
    assert (paths.general_live_dir() / "orchestrator.pid").exists()


def test_kill_stale_orchestrator_ignores_non_orchestrator_processes(tmp_path, monkeypatch):
    import orchestrator.main as om

    root = str(Path(om.__file__).resolve().parent.parent)
    not_orch = _fake_proc(991004, cwd=root, cmdline=("python", "-m", "some.other.module"))

    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [not_orch])
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))

    om._kill_stale_orchestrator()
    not_orch.terminate.assert_not_called()


def test_pidfile_resolves_under_general_live_dir(tmp_path, monkeypatch):
    """The orchestrator PID file lives in the shared general live folder, not the worktree."""
    import orchestrator.main as om
    import paths
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))
    assert om._pidfile() == paths.general_live_dir() / "orchestrator.pid"


# ---------------------------------------------------------------------------
# trade.py._terminate_all  (powershell wrapper + automation.main scans)
# ---------------------------------------------------------------------------

def test_terminate_all_scopes_scans_to_this_worktree(tmp_path, monkeypatch):
    import trade

    monkeypatch.chdir(tmp_path)
    # PID file resolves under paths.general_live_dir(); point it at an empty tmp so the
    # pid-file graceful-stop block is skipped (no orchestrator.pid there).
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))
    root = str(Path(trade.__file__).resolve().parent)  # this worktree's root
    other = str(tmp_path / "other_worktree")

    ps_same    = _fake_proc(992001, cwd=root,  name="powershell.exe",
                            cmdline=("powershell", "-m", "orchestrator.main"))   # → terminate
    ps_other   = _fake_proc(992002, cwd=other, name="powershell.exe",
                            cmdline=("powershell", "-m", "orchestrator.main"))   # other worktree → skip
    auto_same  = _fake_proc(992003, cwd=root,  cmdline=("python", "-m", "automation.main"))   # → terminate
    auto_other = _fake_proc(992004, cwd=other, cmdline=("python", "-m", "automation.main"))   # other → skip

    monkeypatch.setattr("psutil.process_iter",
                        lambda attrs=None: [ps_same, ps_other, auto_same, auto_other])

    trade._terminate_all()

    ps_same.terminate.assert_called_once()
    ps_other.terminate.assert_not_called()
    auto_same.terminate.assert_called_once()
    auto_other.terminate.assert_not_called()


def test_terminate_all_kills_orphan_orchestrator_scoped(tmp_path, monkeypatch):
    """Bug 1: an orphaned `python -m orchestrator.main` whose pid is NOT in the pid file
    (crash-loop / stale-pid) must still be terminated by _terminate_all — scoped to THIS
    worktree, so a sibling worktree's live orchestrator is left alone."""
    import trade

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path / "g"))  # no pid file → pid-branch skipped
    root = str(Path(trade.__file__).resolve().parent)
    other = str(tmp_path / "other_worktree")

    orch_same  = _fake_proc(993001, cwd=root,  cmdline=("python", "-m", "orchestrator.main"))   # → terminate
    orch_other = _fake_proc(993002, cwd=other, cmdline=("python", "-m", "orchestrator.main"))   # other → skip

    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [orch_same, orch_other])

    killed = trade._terminate_all()

    orch_same.terminate.assert_called_once()
    orch_other.terminate.assert_not_called()
    assert any("993001" in k for k in killed)
