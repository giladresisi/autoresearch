"""§11.11 CANDIDATE: `extreme_reject_close` goes quiet for the rest of the plan once one of
its OWN positions has stopped out (`ACT_SEC7_QUIET_AFTER_STOP`).

Runs the real `Executor.on_bar` with the market mechanisms stubbed, like
`test_executor_live_rules.py`, whose helpers it reuses."""
import pytest

from agent.trader import executor as executor_mod
from agent.trader.test_executor_live_rules import _fire, _kinds, _recs, _step, make_executor


@pytest.fixture
def quiet_on(monkeypatch):
    monkeypatch.setenv(executor_mod.SEC7_QUIET_ENV_FLAG, "1")


def _wire(ex):
    """Each machine returns what `nxt[name]` holds on the next bar close; the arbiter
    takes the first non-None fire, in the Executor's own order."""
    nxt = {"sec6": None, "sec7": None, "tmso": None}

    def machine(name):
        def run(now, *a, **k):
            fire, nxt[name] = nxt[name], None
            return fire
        return run

    ex._market.sec6_on_bar_close = machine("sec6")
    ex._market.sec7_on_bar_close = machine("sec7")
    ex._market.tmso_on_bar_close = machine("tmso")
    # O3's pre-open window (09:30-10:30) asks this one too; the base stub lacks it.
    ex._market.micro_smt_entry_on_bar_close = lambda *a, **k: None
    ex._market.pick = lambda fires: next((f for _, f in fires if f is not None), None)
    return nxt


def _stopped_out_by(ex, tmp_path, nxt, mechanism):
    """A fill at 09:40:00 by `mechanism`, stopped out on the 09:45 bar."""
    key = {"extreme_reject_close": "sec7", "tmso_reject": "tmso"}[mechanism]
    _step(ex, "09:39:00", 29250.0)
    nxt[key] = _fire(executor_mod.pd.Timestamp("2026-09-03 09:40", tz="America/New_York"),
                     29250.0, mechanism=mechanism)
    _step(ex, "09:40:00", 29250.0)
    assert ex.position() is not None
    _step(ex, "09:45:00", 29240.0, lo=29230.0)
    assert ex.position() is None and ex.attempts_used() == 1


def test_sec7_is_quiet_after_its_own_stop_out(tmp_path, monkeypatch, quiet_on):
    ex = make_executor(tmp_path, monkeypatch)
    nxt = _wire(ex)
    _stopped_out_by(ex, tmp_path, nxt, "extreme_reject_close")
    nxt["sec7"] = _fire(executor_mod.pd.Timestamp("2026-09-03 09:50", tz="America/New_York"),
                        29260.0, mechanism="extreme_reject_close")
    _step(ex, "09:50:00", 29260.0)
    assert ex.position() is None and _kinds(tmp_path).count("fill") == 1
    vetoes = [r for r in _recs(tmp_path) if r["kind"] == "veto"]
    assert [(v["mechanism"], v["reason"]) for v in vetoes] == [
        ("extreme_reject_close", "sec7_quiet_after_stop")]
    assert ex.attempts_used() == 1, "a suppressed fire spends no attempt"


def test_another_mechanisms_stop_out_does_not_quiet_sec7(tmp_path, monkeypatch, quiet_on):
    ex = make_executor(tmp_path, monkeypatch)
    nxt = _wire(ex)
    _stopped_out_by(ex, tmp_path, nxt, "tmso_reject")
    nxt["sec7"] = _fire(executor_mod.pd.Timestamp("2026-09-03 09:50", tz="America/New_York"),
                        29260.0, mechanism="extreme_reject_close")
    _step(ex, "09:50:00", 29260.0)
    assert ex.position() is not None
    assert ex.position()["artifact_id"] == "extreme_reject_close"


def test_the_other_mechanisms_still_enter_while_sec7_is_quiet(tmp_path, monkeypatch,
                                                               quiet_on):
    ex = make_executor(tmp_path, monkeypatch)
    nxt = _wire(ex)
    _stopped_out_by(ex, tmp_path, nxt, "extreme_reject_close")
    t = executor_mod.pd.Timestamp("2026-09-03 09:50", tz="America/New_York")
    nxt["sec7"] = _fire(t, 29260.0, mechanism="extreme_reject_close")
    nxt["sec6"] = _fire(t, 29260.0, mechanism="fvg_1m_post_extreme")
    _step(ex, "09:50:00", 29260.0)
    assert ex.position() is not None
    fills = [r for r in _recs(tmp_path) if r["kind"] == "fill"]
    assert fills[-1]["mechanism"] == "fvg_1m_post_extreme"


def test_switched_off_sec7_fires_again_after_its_stop_out(tmp_path, monkeypatch):
    monkeypatch.setenv(executor_mod.SEC7_QUIET_ENV_FLAG, "0")
    ex = make_executor(tmp_path, monkeypatch)
    nxt = _wire(ex)
    _stopped_out_by(ex, tmp_path, nxt, "extreme_reject_close")
    nxt["sec7"] = _fire(executor_mod.pd.Timestamp("2026-09-03 09:50", tz="America/New_York"),
                        29260.0, mechanism="extreme_reject_close")
    _step(ex, "09:50:00", 29260.0)
    assert ex.position() is not None and _kinds(tmp_path).count("fill") == 2


@pytest.mark.parametrize("raw, on", [("0", False), ("false", False), ("no", False),
                                     ("off", False), ("1", True), ("true", True),
                                     ("yes", True), ("on", True)])
def test_the_switch_reads_the_usual_words(monkeypatch, raw, on):
    monkeypatch.setenv(executor_mod.SEC7_QUIET_ENV_FLAG, raw)
    assert executor_mod.sec7_quiet_enabled() is on


def test_unset_means_the_module_default(monkeypatch):
    monkeypatch.delenv(executor_mod.SEC7_QUIET_ENV_FLAG, raising=False)
    assert executor_mod.sec7_quiet_enabled() is executor_mod.SEC7_QUIET_DEFAULT
