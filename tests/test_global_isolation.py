"""Verify the conftest autouse fixture isolates the shared global state (O6).

The bug it guards: a test calling `save_global()` in live/disk mode (_IN_MEMORY=False,
the default) writes to `paths.general_live_dir()/global.json` — the REAL shared file the
running orchestrator reads — because that path comes from ACT_GLOBAL_DIR, which the old
`_isolate` fixtures never redirected. The conftest `_isolate_global_state` autouse fixture
now points ACT_GLOBAL_DIR at a per-test temp dir. These tests deliberately do NOT set
ACT_GLOBAL_DIR themselves, so they exercise that fixture.
"""
import paths
import smt_state


def test_general_live_dir_redirected_to_tmp(tmp_path):
    """general_live_dir() (where global.json lives) must resolve under the per-test temp dir,
    never the real ~/projects/auto-co-trader/global."""
    live = paths.general_live_dir()
    assert live == tmp_path / "_global" / "general" / "live"
    assert "projects" not in str(live) or str(tmp_path) in str(live)


def test_save_global_lands_in_tmp_not_shared(tmp_path):
    """save_global() in the default (live/disk) mode must write into the temp global root —
    proving a stray test write can no longer clobber the shared live global.json."""
    smt_state.save_global({"all_time_high": 12345.0, "confidence": "medium", "trend": "up"})
    written = tmp_path / "_global" / "general" / "live" / "global.json"
    assert written.exists(), "global.json was not written under the isolated temp global root"
    assert smt_state.load_global()["all_time_high"] == 12345.0
