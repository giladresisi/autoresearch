# tests/test_session_pipeline_resume_entry.py
# Feature: evaluate/arm an entry immediately on resume + late startup.
#
# On a pause->resume transition (and on a flat late startup) the pipeline arms a force-eval
# (self._force_entry_eval_after = now) so the strategy re-runs its FULL existing entry
# evaluation against the last completed 5m bar IMMEDIATELY (this bar) rather than waiting for
# the next 5m boundary. It reuses the existing _force_entry_eval_after machinery and the REAL
# strategy.run_strategy confirmation + placement logic. These tests drive the real strategy
# (NOT mocked) and assert the resulting new-stop-entry / non-entry.
#
# Isolation: the _isolate_state fixture redirects all state (including the pause sentinel,
# which lives under general_live_dir() == <ACT_GLOBAL_DIR>/general/live) to tmp_path. Nothing
# touches any live session dir.

from __future__ import annotations

import copy

import pandas as pd
import pytest

import smt_state
from session_pipeline import SessionPipeline


# --- local fixtures / helpers (independent of test_session_pipeline.py) -------------------

@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)


def _make_1m_bars(start, n, base=21000.0):
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    return pd.DataFrame({
        "Open": [base] * n, "High": [base + 10.0] * n,
        "Low": [base - 10.0] * n, "Close": [base + 2.0] * n,
        "Volume": [100] * n,
    }, index=idx)


def _resume_pipeline(monkeypatch):
    """SessionPipeline whose hypothesis/trend/daily/SMT passes are no-ops (so hand-set state
    survives), but strategy.run_strategy runs FOR REAL."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod

    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: None)

    hist_mnq = _make_1m_bars("2025-11-13 09:20", n=5)
    hist_mes = _make_1m_bars("2025-11-13 09:20", n=5)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: None)
    monkeypatch.setattr(pipeline, "_run_smt_v2_detection", lambda *a, **kw: [])
    return pipeline


def _default_position():
    return copy.deepcopy(smt_state.DEFAULT_POSITION)


def _seed_up_hypothesis_flat(all_time_high=30000.0):
    smt_state.save_hypothesis({**smt_state.DEFAULT_HYPOTHESIS, "direction": "up"})
    smt_state.save_position(_default_position())
    smt_state.save_global({**smt_state.DEFAULT_GLOBAL, "all_time_high": all_time_high})


def _df(idx_start, o, h, lo, c):
    idx = pd.date_range(idx_start, periods=len(o), freq="1min", tz="America/New_York")
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c,
                         "Volume": [100] * len(o)}, index=idx)


def _today_down_5m():
    """today_mnq ending in a small DOWN 5m window [09:45,09:50) then a resume bar at 09:50.

    Down window 09:45..09:49: Open 21010 -> Close 21000 (body 10 <= MAX_CONFIRMATION_BODY_PTS
    = 25), window high 21012 / low 20998.
    """
    return _df("2025-11-14 09:45",
               o=[21010, 21006, 21005, 21004, 21003, 21002],
               h=[21012, 21010, 21009, 21008, 21007, 21008],
               lo=[21004, 21002, 21001, 21000, 20998, 21000],
               c=[21008, 21006, 21005, 21003, 21000, 21007])


def _mes_today(start="2025-11-14 09:45", n=6):
    return _make_1m_bars(start, n, base=3000.0)


def _resume_bar_mnq():
    # open 21002, high 21008 (< entry 21012 -> resting stop, not market downgrade),
    # close 21007 -> CPR (21007-21000)/8 = 0.875 >= 0.40.
    return pd.Series({"Open": 21002.0, "High": 21008.0, "Low": 21000.0, "Close": 21007.0})


def _resume_bar_mes():
    return pd.Series({"Open": 3000.0, "High": 3008.0, "Low": 2998.0, "Close": 3005.0})


def _start_paused(pipeline):
    """on_session_start with the pause sentinel present (no late-start arm), then hand-set
    the up/flat state."""
    smt_state.pause_path().write_text("paused")
    pipeline.on_session_start(pd.Timestamp("2025-11-14 09:20", tz="America/New_York"),
                              _make_1m_bars("2025-11-14 09:20", n=1))
    _seed_up_hypothesis_flat()


# --- positive: resume arms the entry immediately --------------------------------------------

def test_resume_arms_entry_immediately_against_last_5m_bar(_isolate_state, monkeypatch):
    pipeline = _resume_pipeline(monkeypatch)
    _start_paused(pipeline)

    today_mnq, today_mes = _today_down_5m(), _mes_today()

    # Bar 09:48 (still paused): entry work suppressed, _prev_paused -> True.
    smt_state.pause_path().write_text("paused")
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:48", tz="America/New_York"),
                       _resume_bar_mnq(), _resume_bar_mes(),
                       today_mnq.iloc[:4], today_mes.iloc[:4])
    assert pipeline._prev_paused is True
    assert smt_state.load_position().get("stop_entry", "") == ""

    # Resume: remove sentinel, feed next bar.
    smt_state.pause_path().unlink()
    events = pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:50", tz="America/New_York"),
                                _resume_bar_mnq(), _resume_bar_mes(), today_mnq, today_mes)

    stop_entries = [e for e in events if e.get("kind") == "new-stop-entry"]
    assert stop_entries, f"expected a new-stop-entry armed on resume; got {events}"
    sig = stop_entries[0]
    assert sig["direction"] == "up"
    # entry_price = max(body_high 21010, bar_open 21002 + MIN_APPROACH 10 = 21012) = 21012
    assert sig["price"] == pytest.approx(21012.0)
    pos = smt_state.load_position()
    assert pos["stop_entry"] == pytest.approx(21012.0)
    assert not pos["active"], "entry is armed (resting), not filled"


def test_resume_at_non_5m_boundary_still_arms_immediately(_isolate_state, monkeypatch):
    """Force-eval fires from the resume detection even on a NON-5m resume bar."""
    pipeline = _resume_pipeline(monkeypatch)
    _start_paused(pipeline)

    # Down 5m window [09:45,09:50); resume on a non-5m bar at 09:52 -> last completed 5m
    # window is still [09:45,09:50) (the DOWN bar).
    today_mnq = _df("2025-11-14 09:45",
                    o=[21010, 21006, 21005, 21004, 21003, 21002, 21002, 21002],
                    h=[21012, 21010, 21009, 21008, 21007, 21008, 21008, 21008],
                    lo=[21004, 21002, 21001, 21000, 20998, 21000, 21000, 21000],
                    c=[21008, 21006, 21005, 21003, 21000, 21002, 21002, 21002])
    today_mes = _mes_today(n=8)

    pipeline._prev_paused = True
    smt_state.pause_path().write_text("paused")
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:51", tz="America/New_York"),
                       _resume_bar_mnq(), _resume_bar_mes(),
                       today_mnq.iloc[:7], today_mes.iloc[:7])
    smt_state.pause_path().unlink()

    now = pd.Timestamp("2025-11-14 09:52", tz="America/New_York")
    assert now.minute % 5 != 0
    events = pipeline.on_1m_bar(now, _resume_bar_mnq(), _resume_bar_mes(), today_mnq, today_mes)
    assert any(e.get("kind") == "new-stop-entry" for e in events), \
        f"resume on a non-5m bar must still arm immediately; got {events}"


# --- negatives ------------------------------------------------------------------------------

def test_resume_same_direction_last_bar_arms_nothing(_isolate_state, monkeypatch):
    pipeline = _resume_pipeline(monkeypatch)
    _start_paused(pipeline)
    # UP window (same direction as the up hypothesis) -> no opposite confirmation bar.
    today_mnq = _df("2025-11-14 09:45",
                    o=[21000, 21002, 21004, 21006, 21008, 21010],
                    h=[21004, 21006, 21008, 21010, 21012, 21014],
                    lo=[20998, 21000, 21002, 21004, 21006, 21008],
                    c=[21002, 21004, 21006, 21008, 21010, 21011])
    today_mes = _mes_today()
    mnq = pd.Series({"Open": 21011.0, "High": 21013.0, "Low": 21009.0, "Close": 21012.0})

    pipeline._prev_paused = True
    smt_state.pause_path().unlink(missing_ok=True)
    events = pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:50", tz="America/New_York"),
                                mnq, _resume_bar_mes(), today_mnq, today_mes)
    assert not any(e.get("kind") in ("new-stop-entry", "market-entry") for e in events), \
        f"same-direction last bar must arm nothing; got {events}"
    assert smt_state.load_position().get("stop_entry", "") == ""


def test_resume_too_big_body_arms_nothing(_isolate_state, monkeypatch):
    pipeline = _resume_pipeline(monkeypatch)
    _start_paused(pipeline)
    # DOWN window with a 40-pt body (21040 -> 21000) > MAX_CONFIRMATION_BODY_PTS (25).
    today_mnq = _df("2025-11-14 09:45",
                    o=[21040, 21030, 21020, 21010, 21005, 21002],
                    h=[21042, 21035, 21025, 21015, 21008, 21008],
                    lo=[21030, 21020, 21010, 21002, 20998, 21000],
                    c=[21035, 21025, 21015, 21005, 21000, 21002])
    today_mes = _mes_today()

    pipeline._prev_paused = True
    smt_state.pause_path().unlink(missing_ok=True)
    events = pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:50", tz="America/New_York"),
                                _resume_bar_mnq(), _resume_bar_mes(), today_mnq, today_mes)
    assert not any(e.get("kind") in ("new-stop-entry", "market-entry") for e in events), \
        f"too-big confirmation body must arm nothing; got {events}"
    assert smt_state.load_position().get("stop_entry", "") == ""


def test_resume_with_active_position_does_not_force_eval(_isolate_state, monkeypatch):
    pipeline = _resume_pipeline(monkeypatch)
    _start_paused(pipeline)
    pos = _default_position()
    pos["active"] = {"time": "2025-11-14 09:40", "fill_price": 21000.0,
                     "direction": "long", "stop": 20990.0, "contracts": 2, "cautious": "no"}
    smt_state.save_position(pos)

    today_mnq, today_mes = _today_down_5m(), _mes_today()
    pipeline._prev_paused = True
    smt_state.pause_path().unlink(missing_ok=True)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:50", tz="America/New_York"),
                       _resume_bar_mnq(), _resume_bar_mes(), today_mnq, today_mes)
    pos_after = smt_state.load_position()
    assert pos_after.get("active"), "active position must be preserved"
    assert pos_after.get("stop_entry", "") == ""


def test_resume_with_resting_entry_is_not_re_armed(_isolate_state, monkeypatch):
    """Resume while an entry is ALREADY resting: the resume path's guard (stop_entry == '')
    prevents a new force-eval, so the pre-existing resting entry is left untouched."""
    pipeline = _resume_pipeline(monkeypatch)
    _start_paused(pipeline)
    pos = _default_position()
    pos["stop_entry"] = "21500.0"
    pos["stop_direction"] = "up"
    smt_state.save_position(pos)

    today_mnq, today_mes = _today_down_5m(), _mes_today()
    pipeline._prev_paused = True
    smt_state.pause_path().unlink(missing_ok=True)
    pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:51", tz="America/New_York"),
                       _resume_bar_mnq(), _resume_bar_mes(), today_mnq, today_mes)
    # No 5m boundary at 09:51 and the resume path did not arm a force-eval, so the resting
    # entry is unchanged (the real strategy never ran its full entry eval this bar).
    assert smt_state.load_position().get("stop_entry", "") == "21500.0"


# --- late startup ---------------------------------------------------------------------------

def test_late_startup_flat_arms_force_eval_and_first_bar_evaluates(_isolate_state, monkeypatch):
    pipeline = _resume_pipeline(monkeypatch)
    smt_state.pause_path().unlink(missing_ok=True)  # NOT paused
    pipeline.on_session_start(pd.Timestamp("2025-11-14 09:48", tz="America/New_York"),
                              _make_1m_bars("2025-11-14 09:20", n=1))
    assert pipeline._force_entry_eval_after is not None, "flat late startup must arm a force-eval"
    assert pipeline._prev_paused is False

    _seed_up_hypothesis_flat()
    today_mnq, today_mes = _today_down_5m(), _mes_today()
    events = pipeline.on_1m_bar(pd.Timestamp("2025-11-14 09:50", tz="America/New_York"),
                                _resume_bar_mnq(), _resume_bar_mes(), today_mnq, today_mes)
    assert any(e.get("kind") == "new-stop-entry" for e in events), \
        f"late-startup first bar must evaluate + arm the entry; got {events}"


def test_late_startup_while_paused_does_not_arm(_isolate_state, monkeypatch):
    pipeline = _resume_pipeline(monkeypatch)
    smt_state.pause_path().write_text("paused")
    pipeline.on_session_start(pd.Timestamp("2025-11-14 09:48", tz="America/New_York"),
                              _make_1m_bars("2025-11-14 09:20", n=1))
    assert pipeline._force_entry_eval_after is None, \
        "startup while paused must not arm a force-eval"
    assert pipeline._prev_paused is True
