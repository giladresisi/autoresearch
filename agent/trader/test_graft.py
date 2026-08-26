import inspect
import pandas as pd
import pytest


def test_graft_uses_the_additive_hook_not_the_primary_hook():
    """NEW-INSIGHT 3: _trade_primary early-returns and bypasses legacy (one-brain)."""
    src = inspect.getsource(__import__("session_pipeline"))
    idx = src.index("self._trader")
    window = src[idx: idx + 400]
    assert "return []" not in window, "the trader hook must not early-return"


def test_graft_never_imports_live_orders():
    import agent.trader.graft as mod
    assert "live_orders" not in inspect.getsource(mod)


def test_graft_swallows_all_exceptions(monkeypatch, tmp_path):
    from agent.trader.graft import TraderGraft
    g = TraderGraft(state_dir=tmp_path, backend=None)
    monkeypatch.setattr(g, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    g.on_bar(pd.Timestamp("2026-08-13 09:20", tz="America/New_York"),
             pd.DataFrame(), pd.DataFrame())


# --- added during implementation (not in the plan) --------------------------- #

def _bars(start="2026-08-13 00:00", n=600):
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    base = pd.Series(range(n), index=idx).astype(float) * -0.25 + 29900
    return pd.DataFrame({"Open": base, "High": base + 3, "Low": base - 3,
                         "Close": base, "Volume": 1.0}, index=idx)


class _Stub:
    def __init__(self, thesis): self.thesis = thesis; self.calls = 0
    def __call__(self, *a, **k): self.calls += 1; return self.thesis


ARM = pd.Timestamp("2026-08-13 09:20", tz="America/New_York")


def _graft(tmp_path, backend):
    """Non-threaded so the thesis lands synchronously and the tests stay deterministic.
    Production uses threaded=True (see test_analyzer_runs_off_the_bar_loop)."""
    from agent.trader.graft import TraderGraft
    return TraderGraft(state_dir=tmp_path, backend=backend, threaded=False)


def test_a_backendless_graft_is_completely_dark(tmp_path):
    from agent.trader.graft import TraderGraft
    from agent.trader.records import DECISIONS_FILE
    g = TraderGraft(state_dir=tmp_path, backend=None)
    bars = _bars()
    for ts in bars.index[540:560]:
        g.on_bar(ts, bars[bars.index <= ts], bars[bars.index <= ts])
    assert g.plan() is None
    assert not (tmp_path / DECISIONS_FILE).exists()


def test_a_neutral_thesis_leaves_the_chain_dark(tmp_path):
    """l2 §2 (2026-08-17): no standing plan ⇒ place nothing, track state only."""
    g = _graft(tmp_path, _Stub({"bias": "NEUTRAL", "dol": None}))
    bars = _bars()
    for ts in bars.index[(bars.index >= ARM) & (bars.index <= ARM + pd.Timedelta(minutes=30))]:
        g.on_bar(ts, bars[bars.index <= ts], bars[bars.index <= ts])
    assert g.plan() is None


def test_a_standing_thesis_produces_a_plan_and_drives_the_executor(tmp_path):
    from agent.trader.plan_store import PlanStore
    g = _graft(tmp_path, _Stub({"thesis_id": "t1", "bias": "DOWN",
                                "dol": {"level": "prev1_week_low", "price": 29000.0}}))
    bars = _bars()
    for ts in bars.index[(bars.index >= ARM) & (bars.index <= ARM + pd.Timedelta(minutes=20))]:
        g.on_bar(ts, bars[bars.index <= ts], bars[bars.index <= ts])
    plan = g.plan()
    assert plan is not None and plan["direction"] == "DOWN"
    assert PlanStore(tmp_path).get(plan["plan_id"]) is not None
    assert g.bind_state() is not None


def test_the_graft_writes_no_legacy_state_file(tmp_path):
    g = _graft(tmp_path, _Stub({"thesis_id": "t1", "bias": "DOWN",
                                "dol": {"level": "x", "price": 29000.0}}))
    bars = _bars()
    for ts in bars.index[(bars.index >= ARM) & (bars.index <= ARM + pd.Timedelta(minutes=20))]:
        g.on_bar(ts, bars[bars.index <= ts], bars[bars.index <= ts])
    for forbidden in ("events.jsonl", "daily.json", "hypothesis.json", "position.json",
                      "smts.json"):
        assert not (tmp_path / forbidden).exists()


def test_trader_enabled_is_on_unless_explicitly_disabled(monkeypatch):
    from agent.trader.graft import TraderGraft, trader_enabled, TRADER_ENV_FLAG
    monkeypatch.delenv(TRADER_ENV_FLAG, raising=False)
    assert trader_enabled() is True, "unset must mean ON — every session runs the chain"
    monkeypatch.setenv(TRADER_ENV_FLAG, "")
    assert trader_enabled() is True, "empty behaves like unset — ON"
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv(TRADER_ENV_FLAG, off)
        assert trader_enabled() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv(TRADER_ENV_FLAG, on)
        assert trader_enabled() is True


def test_graft_never_calls_a_forbidden_state_mutator():
    import agent.trader.graft as mod
    src = inspect.getsource(mod)
    for forbidden in ("save_daily", "save_position", "save_hypothesis",
                      "freeze_active_mgmt", "set_state_dir", "events.jsonl"):
        assert forbidden not in src


# --- regression tests for the two blocking review findings ------------------- #

def test_chain_runs_on_the_LIVE_cadence_where_bar_complete_is_always_false(tmp_path):
    """automation/main.py:246 and backtest_smt.py:1597 hard-code bar_complete=False on
    EVERY tick — the consumer is expected to notice the minute rollover itself, exactly
    as session_pipeline does for SMT detection. Taking the flag at face value left the
    entire chain inert in live: the Analyzer spent an LLM call at 09:20 and then nothing
    else ever happened. This drives the real live cadence and demands a plan."""
    from agent.trader.records import DECISIONS_FILE
    g = _graft(tmp_path, _Stub({"thesis_id": "t1", "bias": "DOWN",
                                "dol": {"level": "prev1_week_low", "price": 29000.0}}))
    bars = _bars()
    window = bars.index[(bars.index >= ARM) & (bars.index <= ARM + pd.Timedelta(minutes=25))]
    for ts in window:
        for sec in (0, 20, 40):                       # per-second ticks inside the minute
            t = ts + pd.Timedelta(seconds=sec)
            g.on_bar(t, bars[bars.index <= ts], bars[bars.index <= ts], bar_complete=False)
    assert g.plan() is not None, "the live cadence must still derive a plan"
    assert g.bind_state() is not None
    assert g.bind_state()["last_cascade"] is not None


def test_chain_runs_when_bar_complete_is_none(tmp_path):
    """session_pipeline's own default is None ('auto-detect from cadence')."""
    g = _graft(tmp_path, _Stub({"thesis_id": "t1", "bias": "DOWN",
                                "dol": {"level": "x", "price": 29000.0}}))
    bars = _bars()
    for ts in bars.index[(bars.index >= ARM) & (bars.index <= ARM + pd.Timedelta(minutes=10))]:
        g.on_bar(ts, bars[bars.index <= ts], bars[bars.index <= ts])
    assert g.plan() is not None


def test_history_is_spliced_onto_the_session_frame(tmp_path):
    """today_* is the current CME session only; the Analyzer needs 17 days. Without the
    splice detect_levels finds no prior session and the thesis is decided on nothing."""
    captured = {}

    class _Capture:
        calls = 0
        def __call__(self, facts_text, context_text, facts, **k):
            type(self).calls += 1
            captured["facts"] = facts
            return {"thesis_id": "t", "bias": "DOWN", "dol": {"level": "x", "price": 1.0}}

    hist = _bars(start="2026-07-20 00:00", n=60 * 24 * 20)
    today = _bars(start="2026-08-13 00:00", n=600)
    g = _graft(tmp_path, _Capture())
    ts = ARM
    g.on_bar(ts, today[today.index <= ts], today[today.index <= ts],
             hist_mnq=hist, hist_mes=hist)
    assert _Capture.calls == 1
    assert captured["facts"].get("now_price") is not None
    assert not captured["facts"].get("degraded"), \
        "with history spliced the view must not be the degraded empty contract"


def test_without_history_the_view_is_visibly_degraded(tmp_path):
    """The counterpart of the test above: this is what the graft used to do."""
    captured = {}

    class _Capture:
        def __call__(self, facts_text, context_text, facts, **k):
            captured["facts"] = facts
            return {"thesis_id": "t", "bias": "DOWN", "dol": {"level": "x", "price": 1.0}}

    today = _bars(start="2026-08-13 00:00", n=600)
    g = _graft(tmp_path, _Capture())
    g.on_bar(ARM, today[today.index <= ARM], today[today.index <= ARM])
    lvls = (captured["facts"].get("levels") or {})
    assert not [n for n in lvls if n.startswith("prev")], \
        "a session-only frame cannot contain prior-day levels — that is the bug"


def test_analyzer_runs_off_the_bar_loop_when_threaded(tmp_path):
    """The 09:20 call is a synchronous HTTP request on the per-second IB tick callback
    that also drives the legacy engine's ORDER EXECUTION. It must not block it."""
    import threading
    import time
    from agent.trader.graft import TraderGraft

    started = threading.Event()
    release = threading.Event()

    class _Slow:
        def __call__(self, *a, **k):
            started.set()
            release.wait(timeout=10)
            return {"thesis_id": "t", "bias": "DOWN", "dol": {"level": "x", "price": 1.0}}

    g = TraderGraft(state_dir=tmp_path, backend=_Slow(), threaded=True)
    bars = _bars()
    t0 = time.perf_counter()
    g.on_bar(ARM, bars[bars.index <= ARM], bars[bars.index <= ARM])
    elapsed = time.perf_counter() - t0
    assert started.wait(timeout=5), "the call should have been submitted"
    assert elapsed < 2.0, f"the bar loop blocked for {elapsed:.2f}s on the model call"
    assert g.plan() is None, "no thesis has landed yet"
    release.set()
    g._analyzer._thread.join(timeout=10)
    assert g._analyzer.standing_thesis() is not None


def test_the_graft_snapshots_the_session_store(tmp_path):
    """The snapshot follows the store that actually exists.

    It used to be taken of a throwaway store the Analyzer built and then ignored; it is
    now taken of the SESSION store the Executor binds against, once per session date.
    """
    from agent.facts.journal import FACTS_SNAPSHOT_NAME
    g = _graft(tmp_path, _Stub({"thesis_id": "t1", "bias": "DOWN",
                                "dol": {"level": "x", "price": 29000.0}}))
    bars = _bars()
    # Live cadence: the first bar seeds the minute, the second rolls it and cascades.
    # The snapshot describes a cascaded store, so it needs the close.
    g.on_bar(ARM, bars[bars.index <= ARM], bars[bars.index <= ARM])
    nxt = ARM + pd.Timedelta(minutes=1)
    g.on_bar(nxt, bars[bars.index <= nxt], bars[bars.index <= nxt])
    assert (tmp_path / FACTS_SNAPSHOT_NAME).exists()
