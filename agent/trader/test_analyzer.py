import pandas as pd
import pytest
from agent.trader.analyzer import Analyzer, ARM_HOUR, ARM_MINUTE


class _StubBackend:
    def __init__(self, thesis): self.thesis = thesis; self.calls = 0
    def __call__(self, *a, **k): self.calls += 1; return self.thesis


def _bars(start="2026-08-13 00:00", n=600):
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    base = pd.Series(range(n), index=idx).astype(float) * 0.25 + 29000
    return pd.DataFrame({"Open": base, "High": base + 3, "Low": base - 3,
                         "Close": base, "Volume": 1.0}, index=idx)


BARS = {"MNQ": _bars(), "MES": _bars()}


def test_analyzer_does_not_depend_on_the_dead_0920_daily_hook(tmp_path):
    """NEW-INSIGHT 1: session_pipeline's 09:20 branch never fires (midnight wins)."""
    import inspect
    import agent.trader.analyzer as mod
    src = inspect.getsource(mod)
    assert "_last_daily_date" not in src
    assert "on_daily_or_startup" not in src


def test_fires_once_at_0920_bar_time(tmp_path):
    a = Analyzer(state_dir=tmp_path, backend=_StubBackend({"bias": "DOWN"}))
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    assert a.maybe_run(ts, BARS) is not None


def test_does_not_fire_twice_on_the_same_session_date(tmp_path):
    b = _StubBackend({"bias": "DOWN"})
    a = Analyzer(state_dir=tmp_path, backend=b)
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    a.maybe_run(ts + pd.Timedelta(seconds=1), BARS)
    a.maybe_run(ts + pd.Timedelta(minutes=1), BARS)
    assert b.calls == 1


def test_uses_bar_time_never_wall_clock(tmp_path):
    """NEW-INSIGHT 4: wall-clock inside the loop breaks cycle-2 replay."""
    import inspect
    import agent.trader.analyzer as mod
    src = inspect.getsource(mod)
    assert "get_et_now" not in src
    assert "datetime.now" not in src and "Timestamp.now" not in src


def test_reloads_a_persisted_thesis_instead_of_recalling_after_restart(tmp_path):
    b = _StubBackend({"bias": "DOWN", "dol": {"level": "x", "price": 1.0}})
    a = Analyzer(state_dir=tmp_path, backend=b)
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    a2 = Analyzer(state_dir=tmp_path, backend=b)
    assert a2.standing_thesis() is not None
    assert b.calls == 1, "a restart must not re-spend an API call"


def test_neutral_bias_yields_no_standing_thesis(tmp_path):
    a = Analyzer(state_dir=tmp_path, backend=_StubBackend({"bias": "NEUTRAL", "dol": None}))
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    assert a.standing_thesis() is None


def test_missing_dol_yields_no_standing_thesis(tmp_path):
    """Gate is NEUTRAL-or-no-DOL only. Confidence does NOT gate."""
    a = Analyzer(state_dir=tmp_path, backend=_StubBackend({"bias": "DOWN", "dol": None}))
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    assert a.standing_thesis() is None


def test_low_confidence_directional_thesis_still_stands(tmp_path):
    """Explicitly NOT gated on confidence — the calibration table is out of scope."""
    a = Analyzer(state_dir=tmp_path, backend=_StubBackend(
        {"bias": "DOWN", "confidence": "LOW", "dol": {"level": "x", "price": 1.0}}))
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    assert a.standing_thesis() is not None


def test_failsafe_produces_a_dark_day_not_degraded_trading(tmp_path):
    class _Boom:
        calls = 0
        def __call__(self, *a, **k): raise RuntimeError("api down")
    a = Analyzer(state_dir=tmp_path, backend=_Boom())
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    assert a.standing_thesis() is None


def test_ignores_the_fact_store_entirely(tmp_path):
    """The Analyzer is self-contained AND store-free.

    It used to build a private FactStore and drive the full lookback over it purely to
    stamp a health snapshot — while `assemble_facts` ignored that store by design. The
    store belongs to the Executor; the Analyzer assembles its own point-in-time view
    and reads no store at all.
    """
    import inspect
    import agent.trader.analyzer as mod
    src = inspect.getsource(mod)
    assert "FactStore(" not in src
    assert "run_batch" not in src
    assert "shared_store" not in src


def test_writes_no_legacy_state_file(tmp_path):
    a = Analyzer(state_dir=tmp_path, backend=_StubBackend({"bias": "DOWN"}))
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    for forbidden in ("events.jsonl", "daily.json", "hypothesis.json",
                      "position.json", "smts.json"):
        assert not (tmp_path / forbidden).exists()


# --- added during implementation (not in the plan) --------------------------- #

def test_a_raising_backend_still_burns_the_arm_so_it_cannot_re_fire(tmp_path):
    class _Boom:
        calls = 0
        def __call__(self, *a, **k):
            type(self).calls += 1
            raise RuntimeError("api down")
    b = _Boom()
    a = Analyzer(state_dir=tmp_path, backend=b)
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    a.maybe_run(ts + pd.Timedelta(seconds=1), BARS)
    assert _Boom.calls == 1, "a failed call must not be retried inside the arm minute"


def test_does_not_fire_outside_the_arm_minute(tmp_path):
    b = _StubBackend({"bias": "DOWN"})
    a = Analyzer(state_dir=tmp_path, backend=b)
    for ts in ("2026-08-13 09:19", "2026-08-13 09:21", "2026-08-13 08:20",
               "2026-08-13 18:00"):
        a.maybe_run(pd.Timestamp(ts, tz="America/New_York"), BARS)
    assert b.calls == 0


def test_arms_again_on_the_next_session_date(tmp_path):
    b = _StubBackend({"bias": "DOWN", "dol": {"level": "x", "price": 1.0}})
    a = Analyzer(state_dir=tmp_path, backend=b)
    for day in ("2026-08-13", "2026-08-14"):
        a.maybe_run(pd.Timestamp(f"{day} {ARM_HOUR:02d}:{ARM_MINUTE:02d}",
                                 tz="America/New_York"), BARS)
    assert b.calls == 2


def test_records_facts_health_provenance(tmp_path):
    """Provenance now describes the VIEW that was actually sent to the model, not a
    store that had no bearing on the decision."""
    a = Analyzer(state_dir=tmp_path, backend=_StubBackend({"bias": "DOWN"}))
    ts = pd.Timestamp(f"2026-08-13 {ARM_HOUR:02d}:{ARM_MINUTE:02d}", tz="America/New_York")
    a.maybe_run(ts, BARS)
    health = a.facts_health()
    assert health is not None
    for key in ("boundary", "degraded", "facts_text_chars", "levels", "now_price"):
        assert key in health, key


def test_analyzer_never_imports_live_orders():
    import inspect
    import agent.trader.analyzer as mod
    assert "live_orders" not in inspect.getsource(mod)
