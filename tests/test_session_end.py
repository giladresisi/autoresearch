"""`trade.py session-end` (scripts/session_end.py): the fixed rules that replace the
parquet-check skill's judgement — IB gate, merge retry, exit-code mapping."""
from scripts import session_end


def _report(merge=True, promote=True, publish=True):
    return {
        "dry_run": False,
        "instruments": {"MNQ": {"severity": "ok", "action": "merge", "merge_success": merge},
                        "MES": {"severity": "ok", "action": "merge", "merge_success": True}},
        "promotion": ({"promote_success": True, "publish_success": publish,
                       "publish_configured": True} if promote else None),
        "instruments_1m": {"MNQ": {"action": "ok", "repair_success": None}},
        "rollover": {"due": False},
    }


def _patch(monkeypatch, ib_ok, runs):
    calls = []
    monkeypatch.setattr(session_end, "ib_reachable", lambda timeout=3.0: (ib_ok, "h:1"))

    def fake_engine(extra):
        calls.append(list(extra))
        return runs[min(len(calls), len(runs)) - 1]

    monkeypatch.setattr(session_end, "run_engine", fake_engine)
    return calls


def test_happy_path_benign_exit_1_is_success(monkeypatch):
    calls = _patch(monkeypatch, True, [(1, _report())])
    out = []
    assert session_end.run([], out=out.append, sleep=lambda s: None) == 0
    assert calls == [[]]
    assert any("R2 publish: success=True" in l for l in out)


def test_ib_down_refuses_without_running_engine(monkeypatch):
    calls = _patch(monkeypatch, False, [(1, _report())])
    out = []
    assert session_end.run([], out=out.append) == session_end.EXIT_IB_DOWN
    assert calls == []
    assert out and out[0].startswith("REFUSED")


def test_dry_run_skips_ib_gate_and_never_retries(monkeypatch):
    calls = _patch(monkeypatch, False, [(2, _report(merge=False, promote=False))])
    assert session_end.run(["--dry-run"], out=lambda s: None, sleep=lambda s: None) == 2
    assert calls == [["--dry-run"]]


def test_failed_merge_is_retried_after_the_pacing_wait(monkeypatch):
    calls = _patch(monkeypatch, True, [(2, _report(merge=False, promote=False)),
                                      (1, _report())])
    slept = []
    assert session_end.run([], out=lambda s: None, sleep=slept.append) == 0
    assert len(calls) == 2
    assert slept == [session_end.RETRY_WAIT_SEC]


def test_retries_are_bounded(monkeypatch):
    calls = _patch(monkeypatch, True, [(2, _report(merge=False, promote=False))])
    slept = []
    rc = session_end.run([], retries=2, out=lambda s: None, sleep=slept.append)
    assert rc == 2
    assert len(calls) == 3 and len(slept) == 2


def test_publish_failure_is_not_retried_and_fails(monkeypatch):
    calls = _patch(monkeypatch, True, [(2, _report(publish=False))])
    out = []
    assert session_end.run([], out=out.append, sleep=lambda s: None) == 2
    assert len(calls) == 1
    assert any("R2 publish: success=False" in l for l in out)


def test_rollover_due_is_surfaced(monkeypatch):
    rep = {"rollover_blocked": True, "exit_code": 4}
    _patch(monkeypatch, True, [(4, rep)])
    out = []
    assert session_end.run([], out=out.append, sleep=lambda s: None) == 4
    assert any("ROLLOVER DUE" in l for l in out)
