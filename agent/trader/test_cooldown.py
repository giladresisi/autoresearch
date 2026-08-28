import pandas as pd

from agent.trader.executor import Executor

TZ = "America/New_York"


def _ex(tmp_path, direction="UP"):
    return Executor(str(tmp_path), {"plan_id": "t", "direction": direction,
                                    "dol": {"price": 30001.5},
                                    "armed_classes": ["fvg_return_continuation"]},
                    arm_ts=pd.Timestamp("2026-08-13 09:20", tz=TZ))


def test_no_cooldown_before_any_stop_out(tmp_path):
    assert _ex(tmp_path)._in_cooldown(pd.Timestamp("2026-08-13 09:32", tz=TZ)) is False


def test_the_cooldown_holds_within_the_stop_out_bars_minute(tmp_path):
    ex = _ex(tmp_path)
    ex._sim.last_stop_out = {"time": pd.Timestamp("2026-08-13 09:33:12", tz=TZ)}
    assert ex._in_cooldown(pd.Timestamp("2026-08-13 09:33:40", tz=TZ)) is True


def test_the_cooldown_ends_when_that_1m_bar_closes(tmp_path):
    """'until the 1m bar in which the stop was hit closes' — 09:33 closes at 09:34:00."""
    ex = _ex(tmp_path)
    ex._sim.last_stop_out = {"time": pd.Timestamp("2026-08-13 09:33:12", tz=TZ)}
    assert ex._in_cooldown(pd.Timestamp("2026-08-13 09:34:00", tz=TZ)) is False


def test_a_stop_out_exactly_on_a_minute_boundary_still_gets_its_bar(tmp_path):
    ex = _ex(tmp_path)
    ex._sim.last_stop_out = {"time": pd.Timestamp("2026-08-13 09:33:00", tz=TZ)}
    assert ex._in_cooldown(pd.Timestamp("2026-08-13 09:33:59", tz=TZ)) is True
    assert ex._in_cooldown(pd.Timestamp("2026-08-13 09:34:00", tz=TZ)) is False


def test_a_long_trigger_below_price_is_crossed(tmp_path):
    assert _ex(tmp_path)._crossed(trigger=29900.0, price=29910.0) is True


def test_a_long_trigger_above_price_is_not_crossed(tmp_path):
    assert _ex(tmp_path)._crossed(trigger=29900.0, price=29890.0) is False


def test_a_short_trigger_above_price_is_crossed(tmp_path):
    assert _ex(tmp_path, direction="DOWN")._crossed(trigger=29900.0,
                                                    price=29890.0) is True


def test_a_crossed_trigger_at_cooldown_end_fires_a_market_entry(tmp_path):
    """07-24: the breakdown crossed the trigger 19 s after the stop; a
    fresh-precondition lockout watched the -280 collapse flat."""
    ex = _ex(tmp_path, direction="DOWN")
    ex._sim.last_stop_out = {"time": pd.Timestamp("2026-08-13 09:33:12", tz=TZ)}
    ex._state.update({"trigger": 29900.0, "now_price": 29850.0, "stop": 29920.0,
                      "bound_id": "g1", "bound_label": "g1",
                      "mechanism": "fvg_return_continuation"})
    ex._resolve_cooldown_end(pd.Timestamp("2026-08-13 09:34:00", tz=TZ))
    assert ex._sim.position is not None
    assert ex._sim.position["entry"] == 29850.0


def test_an_uncrossed_trigger_at_cooldown_end_places_a_resting_order(tmp_path):
    ex = _ex(tmp_path, direction="DOWN")
    ex._sim.last_stop_out = {"time": pd.Timestamp("2026-08-13 09:33:12", tz=TZ)}
    ex._state.update({"trigger": 29900.0, "now_price": 29950.0, "stop": 29920.0,
                      "bound_id": "g1", "bound_label": "g1",
                      "mechanism": "fvg_return_continuation"})
    ex._resolve_cooldown_end(pd.Timestamp("2026-08-13 09:34:00", tz=TZ))
    assert ex._sim.position is None
    assert ex._sim.resting is not None


def test_no_entry_is_placed_or_triggered_during_the_cooldown(tmp_path):
    ex = _ex(tmp_path)
    ex._sim.last_stop_out = {"time": pd.Timestamp("2026-08-13 09:33:12", tz=TZ)}
    ex._rebind_and_guard(pd.Timestamp("2026-08-13 09:33:40", tz=TZ))
    assert ex._sim.resting is None and ex._sim.position is None


def test_the_cooldown_does_not_require_a_fresh_precondition(tmp_path):
    """Unlike the settle window: post-stop state reflects a real displacement that just
    took the stop, and demanding it repeat forfeits the move."""
    import inspect
    src = inspect.getsource(Executor._resolve_cooldown_end)
    assert "fresh" not in src.lower() or "NO fresh" in src
