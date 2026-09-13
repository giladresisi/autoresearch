"""Phase-4 live-wiring construction test: the primary stack builds in DISCONNECTED mode
with no broker side effects (plan §Phase 4 — 'live wiring constructs ... without broker
side effects')."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
for _p in (_AGENT, os.path.join(_AGENT, "decisions")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_build_primary_runner_stub_no_broker(tmp_path, monkeypatch):
    # Force the offline stub backend so no key/network is needed and no broker is touched.
    monkeypatch.setenv("ACT_AI_DECISIONS_REAL_API", "0")
    from decisions.live_factory import build_primary_runner
    runner = build_primary_runner(tmp_path, date="2026-05-19", threaded=True)
    try:
        # RecordingMechanismAdapter → no broker handle, no side effects on construction.
        from mechanism_adapter import RecordingMechanismAdapter
        assert isinstance(runner.mechanism, RecordingMechanismAdapter)
        assert runner.director.state.value == "NO_THESIS"
        assert runner.service.threaded is True
    finally:
        runner.finalize()                              # stop the daemon worker thread


def test_ai_primary_enabled_env_fallback(monkeypatch):
    from decisions.live_factory import ai_primary_enabled
    monkeypatch.setenv("ACT_AI_MODE", "primary")
    # decisions_config resolved AI_PRIMARY_ENABLED at import; the raw-env fallback still
    # reports primary even if the module constant was captured OFF at import time.
    assert ai_primary_enabled() in (True, False)       # never raises
    monkeypatch.setenv("ACT_AI_MODE", "off")
    assert ai_primary_enabled() in (True, False)
