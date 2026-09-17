"""Plan 38 Wave 3: the post-session conformance tool. Pure functions plus the one
property that matters most — the replay it runs can never reach a model."""
from __future__ import annotations

import os

import pytest

import scripts.agent_live_conformance as conf


def _rec(kind, mechanism="extreme_reject_close", **kw):
    return {"kind": kind, "time": "2026-09-18T09:40:00-04:00", "mechanism": mechanism,
            "artifact_id": mechanism, **kw}


def test_identical_streams_have_no_delta_even_when_times_and_prices_differ():
    """Gate 2's key is the identity: kind, mechanism, artifact, trigger, stop, reason."""
    live = [_rec("fill", price=29250.0), _rec("stop_out", price=29235.0)]
    replay = [_rec("fill", price=29250.25, time="x"), _rec("stop_out", price=29235.0)]
    assert conf.diff_streams(live, replay) == []


def test_a_live_only_record_a_replay_only_record_and_a_changed_one_are_all_reported():
    live = [_rec("fill"), _rec("fill_voided", reason="entry_refused"), _rec("stop_out")]
    replay = [_rec("fill"), _rec("stop_out"), _rec("plan_dead", reason="window_end")]
    deltas = conf.diff_streams(live, replay)
    assert [d["op"] for d in deltas] == ["live_only", "replay_only"]
    assert deltas[0]["live"][0]["kind"] == "fill_voided" and deltas[0]["replay"] == []
    assert deltas[1]["replay"][0]["reason"] == "window_end"

    changed = conf.diff_streams([_rec("plan_dead", reason="target_reached")],
                                [_rec("plan_dead", reason="attempts_exhausted")])
    assert [d["op"] for d in changed] == ["changed"]


def test_the_key_is_gate_twos_own():
    from agent.trader.test_gate_live_replay_fidelity import _key
    rec = _rec("bind", trigger=1.0, stop=2.0, reason=None)
    assert conf.decision_key(rec) == _key(rec)


def test_the_dispatch_report_surfaces_latency_failures_and_kills():
    lines = [
        {"seq": 1, "bar_time": "t1", "sim_event": {"kind": "fill"},
         "signal": {"kind": "market-entry"}, "ack": {"ok": True, "reason": ""},
         "dispatch_ms": 12.0, "suppressed": None},
        {"seq": 2, "bar_time": "t2", "sim_event": {"kind": "stop_out"},
         "signal": {"kind": "market-close", "reason": "stop_out"},
         "ack": {"ok": False, "reason": "close_not_confirmed"}, "dispatch_ms": 3400.0,
         "suppressed": None},
        {"seq": None, "bar_time": "t3", "sim_event": {"kind": "watchdog_kill"},
         "signal": None, "ack": None, "dispatch_ms": None, "suppressed": None},
        {"seq": 1, "bar_time": "t1", "sim_event": {"kind": "fill"}, "signal": None,
         "ack": {"ok": True, "reason": ""}, "dispatch_ms": None,
         "suppressed": "duplicate"},
    ]
    rep = conf.dispatch_report(lines)
    assert rep["dispatch_ms"] == {"n": 2, "max": 3400.0, "median": 1706.0}
    assert [s["kind"] for s in rep["signals"]] == ["market-entry", "market-close"]
    assert len(rep["ack_failures"]) == 1 and len(rep["watchdog_kills"]) == 1
    assert [l["suppressed"] for l in rep["suppressed"]] == ["duplicate"]
    assert conf.dispatch_report([])["dispatch_ms"] is None
    text = conf.render("2026-09-18", [], rep, "live", "run")
    text.encode("ascii")
    assert "IDENTICAL" in text and "3400.0 ms" in text


def test_the_replay_is_served_the_live_thesis_and_everything_is_restored(monkeypatch):
    """The real backend factory is swapped for the run and put back afterwards — also
    when the run fails — and the thesis cache never points at the shared directory."""
    import agent.trader.replay as R
    from agent.trader.thesis_cache import CACHE_ENV

    real = R._real_backend
    monkeypatch.delenv(CACHE_ENV, raising=False)
    seen = {}

    def _fake_run(dates, *, allow_calls, arrival_latency_sec):
        seen["served"] = R._real_backend()("facts", "ctx", {})
        seen["cache"] = os.environ.get(CACHE_ENV)
        seen["latency"] = arrival_latency_sec
        assert R._real_backend is not real, "the model-calling factory must be unreachable"
        return {dates[0]: {"run_dir": "RUN"}}

    monkeypatch.setattr(R, "run_replay", _fake_run)
    state = {"thesis": {"bias": "UP"}, "call_meta": {"latency_sec": 61.5}}
    assert conf.replay_with_live_thesis("2026-09-18", state) == "RUN"
    assert seen["served"] == ({"bias": "UP"}, {"latency_sec": 61.5})
    assert seen["cache"] and "conformance_cache_" in seen["cache"]
    assert seen["latency"] == 61.5
    assert R._real_backend is real and CACHE_ENV not in os.environ

    def _boom(*a, **k):
        raise RuntimeError("no tape")
    monkeypatch.setattr(R, "run_replay", _boom)
    with pytest.raises(RuntimeError):
        conf.replay_with_live_thesis("2026-09-18", state)
    assert R._real_backend is real and CACHE_ENV not in os.environ


def test_a_session_that_never_armed_exits_2_without_replaying(tmp_path, monkeypatch,
                                                              capsys):
    monkeypatch.setattr(conf, "replay_with_live_thesis",
                        lambda *a: pytest.fail("must not replay"))
    assert conf.main(["2026-09-18", "--live-dir", str(tmp_path)]) == 2
    assert "never armed" in capsys.readouterr().out
