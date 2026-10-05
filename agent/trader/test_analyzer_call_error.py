"""A thesis call that raises leaves a trace: status + call_error in thesis_state.json."""
import json

import pandas as pd

from agent.trader.analyzer import CALL_ATTEMPTS, Analyzer, THESIS_FILE

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


def test_a_transient_failure_is_retried_and_leaves_no_error(tmp_path, monkeypatch, capsys):
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
    assert ax._call(ARM, {})["bias"] == "DOWN"
    st = _state(tmp_path)
    assert calls["n"] == 2 and st["status"] == "ok" and st["call_error"] is None
    assert "THESIS CALL RETRY 1/2" in capsys.readouterr().out


def test_every_attempt_failing_records_the_attempt_count(tmp_path, monkeypatch):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise ConnectionError("down")

    ax = _analyzer(tmp_path, boom, monkeypatch)
    assert ax._call(ARM, {}) is None
    assert calls["n"] == CALL_ATTEMPTS
    assert _state(tmp_path)["call_error"]["attempts"] == CALL_ATTEMPTS


def test_a_rearm_clears_a_previous_error(tmp_path, monkeypatch):
    ax = _analyzer(tmp_path, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
                   monkeypatch)
    ax._call(ARM, {})
    assert _state(tmp_path)["status"] == "failed"
    ax._arm(ARM + pd.Timedelta(seconds=5), {})
    assert _state(tmp_path)["status"] == "failed"      # still failing: error is re-recorded
    ax._backend = lambda *a, **k: ({"bias": "UP", "regime": "TREND", "confidence": "LOW",
                                    "dol": {"level": "x", "price": 1.0},
                                    "falsified_if": [], "evidence": [{"criterion": "P3"}]},
                                   {})
    ax._arm(ARM + pd.Timedelta(seconds=10), {})
    st = _state(tmp_path)
    assert st["status"] == "ok" and st["call_error"] is None


def test_overdue_notice_prints_once_per_mark_while_the_thesis_is_null(tmp_path, capsys):
    ax = Analyzer(tmp_path, lambda *a, **k: None, threaded=True)
    ax._armed_date, ax._armed_at = ARM.date(), ARM
    for sec in (30, 119):
        ax.maybe_run(ARM + pd.Timedelta(seconds=sec), {})
    assert capsys.readouterr().out == ""
    for sec in (120, 130, 299):
        ax.maybe_run(ARM + pd.Timedelta(seconds=sec), {})
    out = capsys.readouterr().out
    assert out.count("THESIS OVERDUE (PENDING)") == 1 and "09:22" in out
    ax.maybe_run(ARM + pd.Timedelta(seconds=300), {})
    ax.maybe_run(ARM + pd.Timedelta(seconds=301), {})
    assert capsys.readouterr().out.count("THESIS OVERDUE") == 1


def test_no_overdue_notice_once_a_thesis_exists(tmp_path, capsys):
    ax = Analyzer(tmp_path, lambda *a, **k: None, threaded=True)
    ax._armed_date, ax._armed_at = ARM.date(), ARM
    ax._thesis = {"bias": "UP"}
    ax.maybe_run(ARM + pd.Timedelta(minutes=10), {})
    assert capsys.readouterr().out == ""


def test_the_arm_stamp_alone_reads_pending(tmp_path, monkeypatch):
    ax = Analyzer(tmp_path, lambda *a, **k: None, threaded=True)
    ax._armed_date, ax._armed_at = ARM.date(), ARM
    ax._save()
    st = _state(tmp_path)
    assert st["status"] == "pending" and st["call_error"] is None
