"""A thesis call that raises leaves a trace: status + call_error in thesis_state.json."""
import json

import pandas as pd

from agent.trader.analyzer import Analyzer, THESIS_FILE

ARM = pd.Timestamp("2026-10-05 09:20", tz="America/New_York")
_FACTS = {"session_stretch": {}, "menus": {"dol": {}}, "levels": {}, "fvg_zones": {},
          "now_price": 31019.25}


def _analyzer(tmp_path, backend, monkeypatch):
    ax = Analyzer(tmp_path, backend, threaded=False)
    monkeypatch.setattr("agent.trader.analyzer.assemble_facts",
                        lambda store, bars, now: ("facts text", "", _FACTS, {}))
    return ax


def _state(tmp_path):
    return json.loads((tmp_path / THESIS_FILE).read_text(encoding="utf-8"))


def test_a_raising_backend_writes_failed_status_and_call_error(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise ConnectionError("provider unreachable")

    ax = _analyzer(tmp_path, boom, monkeypatch)
    assert ax._call(ARM, {}) is None
    st = _state(tmp_path)
    assert st["thesis"] is None and st["status"] == "failed"
    assert st["call_error"]["type"] == "ConnectionError"
    assert "provider unreachable" in st["call_error"]["message"]
    assert "boom" in st["call_error"]["traceback"]
    assert "[AGENT-LIVE] THESIS CALL FAILED" in capsys.readouterr().out


def test_a_good_call_is_ok_and_a_rearm_clears_a_previous_error(tmp_path, monkeypatch):
    thesis = {"bias": "DOWN", "regime": "TREND", "confidence": "MEDIUM",
              "dol": {"level": "x", "price": 29000.0}, "falsified_if": [],
              "evidence": [{"criterion": "P3"}]}
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return thesis, {"verdict": "clean"}

    ax = _analyzer(tmp_path, flaky, monkeypatch)
    ax._call(ARM, {})
    assert _state(tmp_path)["status"] == "failed"
    ax._arm(ARM + pd.Timedelta(seconds=5), {})
    st = _state(tmp_path)
    assert st["status"] == "ok" and st["call_error"] is None


def test_the_arm_stamp_alone_reads_pending(tmp_path, monkeypatch):
    ax = Analyzer(tmp_path, lambda *a, **k: None, threaded=True)
    ax._armed_date, ax._armed_at = ARM.date(), ARM
    ax._save()
    st = _state(tmp_path)
    assert st["status"] == "pending" and st["call_error"] is None
