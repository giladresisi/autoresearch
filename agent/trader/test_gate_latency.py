import time
import pandas as pd
import pytest
from agent.trader.executor import Executor

BUDGET_MS = 20.0     # per-second path; the loop cadence is 1000 ms


def _bars(n=600):
    idx = pd.date_range("2026-08-13 09:00", periods=n, freq="1min", tz="America/New_York")
    s = pd.Series(range(n), index=idx).astype(float) * -0.5 + 29800
    return pd.DataFrame({"Open": s, "High": s + 4, "Low": s - 4, "Close": s,
                         "Volume": 1.0}, index=idx)


PLAN = {"plan_id": "p1", "thesis_id": "t1", "direction": "DOWN",
        "dol": {"level": "x", "price": 29000.0}, "valid_while": [],
        "armed_classes": ["fvg_return_continuation"], "attempts_used": 0,
        "blacklist": [], "cooldown_until": None}


def _measure(tmp_path, bars, n=200, warm=True):
    """Sample the per-second path.

    `warm=True` first drives a bar CLOSE so the store is populated before measuring.
    Measuring against an empty store would flatter the result: `query`, the running
    extremes replace, and the death check all scale with what the store actually holds,
    and in a live session it holds a session's worth of facts by 09:35.
    """
    ex = Executor(tmp_path, plan=dict(PLAN),
                  arm_ts=pd.Timestamp("2026-08-13 09:20", tz="America/New_York"))
    now = pd.Timestamp("2026-08-13 09:35:00", tz="America/New_York")
    if warm:
        ex.on_bar(now - pd.Timedelta(minutes=2), bars, bar_complete=True)
        ex.on_bar(now - pd.Timedelta(minutes=1), bars, bar_complete=True)
    samples = []
    for i in range(n):
        t0 = time.perf_counter()
        ex.on_bar(now + pd.Timedelta(seconds=i), bars, bar_complete=False)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return samples


def test_per_second_path_stays_within_budget(tmp_path):
    bars = {"MNQ": _bars(), "MES": _bars()}
    samples = _measure(tmp_path, bars)
    p95 = samples[int(len(samples) * 0.95)]
    assert p95 < BUDGET_MS, f"per-second p95 {p95:.2f}ms exceeds {BUDGET_MS}ms budget"


# --- added during implementation (not in the plan) --------------------------- #

def test_per_second_path_stays_within_budget_on_a_full_session_frame(tmp_path):
    """600 bars is 10 hours of 1m. A real CME session hands the loop ~1380, and the
    per-second cost must not scale into the budget on the longer frame."""
    bars = {"MNQ": _bars(1380), "MES": _bars(1380)}
    samples = _measure(tmp_path, bars)
    p95 = samples[int(len(samples) * 0.95)]
    assert p95 < BUDGET_MS, f"per-second p95 {p95:.2f}ms exceeds {BUDGET_MS}ms budget"


def test_a_dead_plan_costs_almost_nothing(tmp_path):
    """Once the plan is dead the Executor must go dark, not keep doing cascade work."""
    dead = dict(PLAN, dol={"level": "x", "price": 29799.0})
    ex = Executor(tmp_path, plan=dead,
                  arm_ts=pd.Timestamp("2026-08-13 09:20", tz="America/New_York"))
    bars = {"MNQ": _bars(1380), "MES": _bars(1380)}
    now = pd.Timestamp("2026-08-13 09:35:00", tz="America/New_York")
    ex.on_bar(now, bars, bar_complete=True)
    assert ex.bind_state()["plan_alive"] is False
    samples = []
    for i in range(200):
        t0 = time.perf_counter()
        ex.on_bar(now + pd.Timedelta(seconds=i), bars, bar_complete=False)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    assert samples[int(len(samples) * 0.95)] < BUDGET_MS
