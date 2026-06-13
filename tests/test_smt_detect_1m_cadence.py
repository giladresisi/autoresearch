# tests/test_smt_detect_1m_cadence.py
# R3: SMT DETECTION must fire only on COMPLETED 1m bars, deterministically across replay
# modes (1m regression, 1s regression, live), while ORDER EXECUTION stays at 1s cadence.
#
# These tests drive a SessionPipeline directly with IDENTICAL underlying 1m bars through
# both the 1m-completed-bar path (bar_complete=True) and the per-second 1s/live path
# (bar_complete=False, each minute replayed as growing intra-minute partials), and assert
# the emitted smt-div stream is byte-identical (modulo the exact sub-minute timestamp).
#
# The full-regression 1m-vs-1s parity is documented in the execution report; any residual
# difference there stems from the 1m parquet and 1s parquet being DIFFERENT data sources,
# not from detection cadence — which these controlled-input tests isolate and prove.

from __future__ import annotations

import pandas as pd
import pytest

import smt_state
from session_pipeline import SessionPipeline


# ── Self-contained fixtures/helpers (mirrors test_session_pipeline.py) ──────────

@pytest.fixture()
def _isolate_state(tmp_path, monkeypatch):
    """Redirect all smt_state paths to tmp_path; disable in-memory mode (live path)."""
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    monkeypatch.setenv("ACT_GLOBAL_DIR", str(tmp_path))
    monkeypatch.setattr(smt_state, "_IN_MEMORY", False)


def _make_1m_bars(start: str, n: int, base: float = 21000.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    return pd.DataFrame({
        "Open":   [base] * n,
        "High":   [base + 10.0] * n,
        "Low":    [base - 10.0] * n,
        "Close":  [base + 2.0] * n,
        "Volume": [100] * n,
    }, index=idx)


def _bar(base, high_off=5.0, low_off=5.0, close_off=1.0):
    return pd.Series({"Open": base, "High": base + high_off,
                      "Low": base - low_off, "Close": base + close_off})


def _smt_v2_pipeline(monkeypatch, emitted=None):
    """A SessionPipeline with daily/trend/hypothesis/strategy stubbed out so only the SMT V2
    additive block runs. Seeded at 19:00 (Asia) force_reset."""
    import daily as _daily_mod
    import trend as _trend_mod
    import hypothesis as _hyp_mod
    import strategy as _strat_mod
    monkeypatch.setattr(_daily_mod, "run_daily_fixed", lambda *a, **kw: None)
    monkeypatch.setattr(_trend_mod, "run_trend", lambda *a, **kw: None)
    monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **kw: [])
    monkeypatch.setattr(_strat_mod, "run_strategy", lambda *a, **kw: None)
    emitted = emitted if emitted is not None else []
    hist_mnq = _make_1m_bars("2025-11-12 09:20", n=5, base=21000.0)
    hist_mes = _make_1m_bars("2025-11-12 09:20", n=5, base=3000.0)
    pipeline = SessionPipeline(hist_mnq, hist_mes, lambda e: emitted.append(e))
    pipeline.on_session_start(
        pd.Timestamp("2025-11-13 19:00", tz="America/New_York"),
        _make_1m_bars("2025-11-13 19:00", n=1), force_reset=True)
    return pipeline, emitted


def _freeze_liquidities(monkeypatch, pipeline):
    """Stop the per-bar dynamic-liquidity passes from overwriting test-injected daily.json,
    and clear the additive universe blocks, so detection sees exactly the crafted levels."""
    monkeypatch.setattr(pipeline, "_update_dynamic_liquidities", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline, "_update_mes_liquidities", lambda *a, **kw: [])
    _d = smt_state.load_daily()
    _d["liquidities_universe"] = []
    _d["liquidities_universe_mes"] = []
    smt_state.save_daily(_d)


def _seed_shared_day_high(pipeline, mnq_price=21000.0, mes_price=3000.0):
    daily = smt_state.load_daily()
    daily["liquidities"] = [{"name": "day_high", "kind": "level", "price": mnq_price}]
    daily["liquidities_mes"] = [{"name": "day_high", "kind": "level", "price": mes_price}]
    smt_state.save_daily(daily)


def _smt_key(e: dict) -> tuple:
    """Identity of an smt-div event modulo the exact sub-minute timestamp: floor time to
    the minute and compare the semantic fields (kind/level/side/leader/type/timeframe)."""
    return (
        pd.Timestamp(e["time"]).floor("1min"),
        e.get("side"), e.get("type"), e.get("timeframe"),
        e.get("leader"), e.get("ref_name"), e.get("phase"),
    )


# ---------------------------------------------------------------------------
# A scripted multi-bar MNQ-vs-MES scenario that fires a wick SMT mid-session.
# Each minute: MNQ wick takes out day_high on the FIRST bar that crosses it; MES never
# does → a single bearish wick divergence. Surrounding bars stay below the level so the
# detector's single-fire/arm logic exercises a realistic edge transition.
# ---------------------------------------------------------------------------

_START = pd.Timestamp("2025-11-13 20:00", tz="America/New_York")
_MNQ_LEVEL = 21000.0
_MES_LEVEL = 3000.0
# Per minute: the MNQ wick High. The MNQ wick takes out _MNQ_LEVEL only on minute 20:02;
# all other minutes stay below. MES never reaches _MES_LEVEL on any minute → an MNQ-led
# bearish wick divergence that fires EXACTLY once, on the take-out minute.
_MNQ_HIGHS = [
    20998.0,  # 20:00 below level (arms)
    20998.0,  # 20:01 below level
    21006.0,  # 20:02 TAKES OUT → bearish wick SMT fires HERE
    20998.0,  # 20:03 below
    20998.0,  # 20:04 below
    20998.0,  # 20:05 (5m boundary) below
]
_N = len(_MNQ_HIGHS)


def _mnq_mes_frames():
    idx = pd.date_range("2025-11-13 20:00", periods=_N, freq="1min", tz="America/New_York")
    mnq = pd.DataFrame({
        "Open":   [20996.0] * _N,
        "High":   list(_MNQ_HIGHS),
        "Low":    [20994.0] * _N,
        "Close":  [20996.0] * _N,
        "Volume": [100.0] * _N,
    }, index=idx)
    # MES stays comfortably below its 3000 level on every bar (High 2995).
    mes = pd.DataFrame({
        "Open":   [2990.0] * _N,
        "High":   [2995.0] * _N,
        "Low":    [2988.0] * _N,
        "Close":  [2990.0] * _N,
        "Volume": [100.0] * _N,
    }, index=idx)
    return mnq, mes


def _fresh_state_dir(monkeypatch, tag):
    """Give a runner its own isolated state dir so a second runner in the same test does
    not inherit the first run's persisted detect_state / smts.json (which would suppress a
    re-fire of an already-fired single-fire SMT)."""
    import os, tempfile, paths
    d = tempfile.mkdtemp(prefix=f"r3_{tag}_")
    monkeypatch.setattr(paths, "_STATE_DIR", d)
    monkeypatch.setenv("ACT_GLOBAL_DIR", d)
    monkeypatch.setenv("ACT_STATE_DIR", d)


def _run_1m_mode(monkeypatch):
    _fresh_state_dir(monkeypatch, "1m")
    pipeline, emitted = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_shared_day_high(pipeline)
    emitted.clear()
    mnq, mes = _mnq_mes_frames()
    out: list[dict] = []
    for i in range(_N):
        now = _START + pd.Timedelta(minutes=i)
        evs = pipeline.on_1m_bar(
            now, mnq.iloc[i], mes.iloc[i],
            mnq.iloc[:i + 1], mes.iloc[:i + 1],
            bar_complete=True,
        )
        out.extend(e for e in evs if e.get("kind") == "smt-div")
    return out


def _run_1s_mode(monkeypatch, secs_per_min=4):
    """Replay the SAME 1m bars as per-second partials (bar_complete=False).

    For each minute we feed `secs_per_min` growing partials whose final partial equals the
    completed 1m bar, then the next minute's first partial triggers the rollover detection
    of the just-completed minute. A finalize_detection() call flushes the last minute."""
    _fresh_state_dir(monkeypatch, "1s")
    pipeline, emitted = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_shared_day_high(pipeline)
    emitted.clear()
    mnq, mes = _mnq_mes_frames()
    out: list[dict] = []

    _cols = ["Open", "High", "Low", "Close", "Volume"]
    _empty = pd.DataFrame(
        columns=_cols, dtype=float,
        index=pd.DatetimeIndex([], tz="America/New_York"))
    base_mnq = _empty.copy()
    base_mes = _empty.copy()

    last_today_mnq = last_today_mes = None
    for i in range(_N):
        minute = _START + pd.Timedelta(minutes=i)
        full_mnq = mnq.iloc[i]
        full_mes = mes.iloc[i]
        for s in range(secs_per_min):
            sec_ts = minute + pd.Timedelta(seconds=s * (60 // secs_per_min))
            frac = (s + 1) / secs_per_min
            # Growing partial: High climbs toward the bar High; the LAST partial == the
            # completed bar so the reconstructed completed bar is exact.
            part_mnq = pd.Series({
                "Open": full_mnq["Open"],
                "High": full_mnq["Open"] + (full_mnq["High"] - full_mnq["Open"]) * frac
                        if s < secs_per_min - 1 else full_mnq["High"],
                "Low": full_mnq["Low"] if s == secs_per_min - 1 else min(full_mnq["Open"], full_mnq["Close"]),
                "Close": full_mnq["Close"] if s == secs_per_min - 1 else full_mnq["Open"],
                "Volume": 100.0,
            })
            part_mes = pd.Series({
                "Open": full_mes["Open"],
                "High": full_mes["High"] if s == secs_per_min - 1 else full_mes["Open"],
                "Low": full_mes["Low"] if s == secs_per_min - 1 else full_mes["Open"],
                "Close": full_mes["Close"] if s == secs_per_min - 1 else full_mes["Open"],
                "Volume": 100.0,
            })
            # today frames: completed base minutes + this minute's running partial row.
            row_mnq = pd.DataFrame([part_mnq[_cols].values], index=[minute], columns=_cols)
            row_mes = pd.DataFrame([part_mes[_cols].values], index=[minute], columns=_cols)
            today_mnq = pd.concat([base_mnq, row_mnq]) if len(base_mnq) else row_mnq
            today_mes = pd.concat([base_mes, row_mes]) if len(base_mes) else row_mes
            evs = pipeline.on_1m_bar(
                sec_ts, part_mnq, part_mes, today_mnq, today_mes, bar_complete=False)
            out.extend(e for e in evs if e.get("kind") == "smt-div")
            last_today_mnq, last_today_mes = today_mnq, today_mes
        # Commit the completed minute into the base accumulators.
        _commit_mnq = pd.DataFrame([full_mnq[_cols].values], index=[minute], columns=_cols)
        _commit_mes = pd.DataFrame([full_mes[_cols].values], index=[minute], columns=_cols)
        base_mnq = pd.concat([base_mnq, _commit_mnq]) if len(base_mnq) else _commit_mnq
        base_mes = pd.concat([base_mes, _commit_mes]) if len(base_mes) else _commit_mes

    # Flush the final pending minute (no successor call rolls it over).
    evs = pipeline.finalize_detection(last_today_mnq, last_today_mes)
    out.extend(e for e in evs if e.get("kind") == "smt-div")
    return out


# ---------------------------------------------------------------------------
# Test 1 (PRIMARY): 1m-completed-bar and per-second paths emit the SAME smt-div stream.
# ---------------------------------------------------------------------------

def test_smt_div_stream_identical_1m_vs_1s(_isolate_state, monkeypatch):
    stream_1m = [_smt_key(e) for e in _run_1m_mode(monkeypatch)]
    stream_1s = [_smt_key(e) for e in _run_1s_mode(monkeypatch)]
    assert stream_1m, "scenario must fire at least one smt-div"
    assert stream_1m == stream_1s, (
        "SMT detection must be deterministic across 1m and 1s cadence given identical "
        f"bars.\n1m: {stream_1m}\n1s: {stream_1s}"
    )


def test_smt_div_fires_on_correct_minute(_isolate_state, monkeypatch):
    """The wick SMT fires on the scripted take-out minute (20:02) in BOTH modes."""
    expect_minute = _START + pd.Timedelta(minutes=2)
    for runner in (_run_1m_mode, _run_1s_mode):
        stream = runner(monkeypatch)
        wick = [e for e in stream if e.get("type") == "wick" and e.get("ref_name") == "day_high"]
        assert len(wick) == 1, f"{runner.__name__}: expected exactly one wick SMT, got {wick}"
        assert pd.Timestamp(wick[0]["time"]).floor("1min") == expect_minute


# ---------------------------------------------------------------------------
# Test 2: detection does NOT run intra-minute in the per-second path.
# ---------------------------------------------------------------------------

def test_no_intra_minute_detection_in_1s_path(_isolate_state, monkeypatch):
    """Feeding multiple intra-minute partials of the SAME minute (no rollover) must NOT
    fire detection — even when an early partial already breaches the level — because the
    bar is not yet complete. Detection only fires once the minute rolls over."""
    pipeline, emitted = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_shared_day_high(pipeline)
    emitted.clear()

    minute = pd.Timestamp("2025-11-13 20:00", tz="America/New_York")
    _cols = ["Open", "High", "Low", "Close", "Volume"]
    # Three partials of minute 20:00, each already breaching day_high (MNQ wick 21006) while
    # MES stays below — a divergence that WOULD fire if detection ran on the partial.
    n_div = 0
    for s in range(3):
        sec_ts = minute + pd.Timedelta(seconds=s * 20)
        part_mnq = _bar(20996.0, high_off=10.0)  # wick 21006 > 21000 level
        part_mes = _bar(2990.0, high_off=5.0)    # below MES level
        row_mnq = pd.DataFrame([[part_mnq["Open"], part_mnq["High"], part_mnq["Low"],
                                 part_mnq["Close"], 100.0]], index=[minute], columns=_cols)
        row_mes = pd.DataFrame([[part_mes["Open"], part_mes["High"], part_mes["Low"],
                                 part_mes["Close"], 100.0]], index=[minute], columns=_cols)
        evs = pipeline.on_1m_bar(sec_ts, part_mnq, part_mes, row_mnq, row_mes,
                                 bar_complete=False)
        n_div += sum(1 for e in evs if e.get("kind") == "smt-div")
    assert n_div == 0, "no SMT must be detected on an intra-minute partial (bar not complete)"

    # Roll over to the next minute → the completed 20:00 bar is now detected.
    next_minute = minute + pd.Timedelta(minutes=1)
    base = pd.DataFrame([[20996.0, 21006.0, 20991.0, 20997.0, 100.0]],
                        index=[minute], columns=_cols)
    part_mnq = _bar(20990.0, high_off=2.0)
    part_mes = _bar(2990.0, high_off=2.0)
    row_mnq = pd.DataFrame([[20990.0, 20992.0, 20985.0, 20990.0, 100.0]],
                           index=[next_minute], columns=_cols)
    row_mes = pd.DataFrame([[2990.0, 2992.0, 2985.0, 2990.0, 100.0]],
                           index=[next_minute], columns=_cols)
    today_mnq = pd.concat([base, row_mnq])
    today_mes = pd.concat([
        pd.DataFrame([[2990.0, 2995.0, 2985.0, 2991.0, 100.0]], index=[minute], columns=_cols),
        row_mes])
    evs = pipeline.on_1m_bar(next_minute, part_mnq, part_mes, today_mnq, today_mes,
                             bar_complete=False)
    sd = [e for e in evs if e.get("kind") == "smt-div"]
    assert sd, "rollover must detect the just-completed minute's bar"
    assert pd.Timestamp(sd[0]["time"]).floor("1min") == minute


# ---------------------------------------------------------------------------
# Test 3: the emit-gate predicate is in place, consulted per completed bar, currently True.
# ---------------------------------------------------------------------------

def test_should_emit_predicate_present_and_currently_true(_isolate_state, monkeypatch):
    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    assert hasattr(pipeline, "_should_emit_smt"), "the emit-gate predicate must exist"
    # Currently lets every completed 1m bar through (no 5m gating yet).
    for m in range(0, 20):
        ts = pd.Timestamp("2025-11-13 20:00", tz="America/New_York") + pd.Timedelta(minutes=m)
        assert pipeline._should_emit_smt(ts) is True


def test_should_emit_predicate_consulted_per_completed_bar(_isolate_state, monkeypatch):
    """The detection driver must consult _should_emit_smt once per COMPLETED bar — the test
    seam for a future 5m-bucket gate. We count calls across the 1m run and assert it is
    invoked at least once per emitted minute (and the run is unchanged when it returns True)."""
    pipeline, emitted = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_shared_day_high(pipeline)
    emitted.clear()

    seen: list = []
    orig = pipeline._should_emit_smt

    def _spy(now):
        seen.append(pd.Timestamp(now).floor("1min"))
        return orig(now)

    monkeypatch.setattr(pipeline, "_should_emit_smt", _spy)

    mnq, mes = _mnq_mes_frames()
    for i in range(_N):
        now = _START + pd.Timedelta(minutes=i)
        pipeline.on_1m_bar(now, mnq.iloc[i], mes.iloc[i],
                           mnq.iloc[:i + 1], mes.iloc[:i + 1], bar_complete=True)

    # One consult per completed bar (each distinct minute appears).
    assert len(set(seen)) == _N, f"predicate must be consulted for each completed bar; saw {seen}"


def test_emit_gate_seam_can_restrict_to_5m(_isolate_state, monkeypatch):
    """Seam check: monkeypatching _should_emit_smt to a 5m-bucket gate suppresses non-5m
    detection without touching detection internals — proving the future tightening is a
    drop-in one-liner. (The production gate stays True; this only exercises the seam.)"""
    pipeline, emitted = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_shared_day_high(pipeline)
    emitted.clear()

    last_bucket = {"v": None}

    def _gate_5m(now):
        b = pd.Timestamp(now).floor("5min")
        if b != last_bucket["v"]:
            last_bucket["v"] = b
            return True
        return False

    monkeypatch.setattr(pipeline, "_should_emit_smt", _gate_5m)

    # Drive the take-out on a NON-5m minute (20:02). With the 5m gate active, that minute is
    # suppressed (its 5m bucket 20:00 was already consumed by 20:00) → no wick SMT emitted.
    mnq, mes = _mnq_mes_frames()
    out: list[dict] = []
    for i in range(_N):
        now = _START + pd.Timedelta(minutes=i)
        evs = pipeline.on_1m_bar(now, mnq.iloc[i], mes.iloc[i],
                                 mnq.iloc[:i + 1], mes.iloc[:i + 1], bar_complete=True)
        out.extend(e for e in evs if e.get("kind") == "smt-div")
    wick = [e for e in out if e.get("type") == "wick" and e.get("ref_name") == "day_high"]
    assert not wick, "5m gate must suppress the non-5m-boundary take-out detection"


# ---------------------------------------------------------------------------
# Test 4: ORDER EXECUTION stays at 1s cadence (run_trend / run_strategy every call).
# ---------------------------------------------------------------------------

def test_execution_runs_every_1s_call(_isolate_state, monkeypatch):
    """Even though SMT detection is gated to completed bars, run_trend and run_strategy
    must still be invoked on EVERY per-second call (no execution-fidelity regression)."""
    import trend as _trend_mod
    import strategy as _strat_mod

    pipeline, _ = _smt_v2_pipeline(monkeypatch)
    _freeze_liquidities(monkeypatch, pipeline)
    _seed_shared_day_high(pipeline)

    # Patch AFTER _smt_v2_pipeline (which installs its own stubs) so these counters win.
    calls = {"trend": 0, "strategy": 0}
    monkeypatch.setattr(_trend_mod, "run_trend",
                        lambda *a, **kw: (calls.__setitem__("trend", calls["trend"] + 1), None)[1])
    monkeypatch.setattr(_strat_mod, "run_strategy",
                        lambda *a, **kw: (calls.__setitem__("strategy", calls["strategy"] + 1), None)[1])

    minute = pd.Timestamp("2025-11-13 20:00", tz="America/New_York")
    _cols = ["Open", "High", "Low", "Close", "Volume"]
    n_secs = 10
    for s in range(n_secs):
        sec_ts = minute + pd.Timedelta(seconds=s * 6)
        row = pd.DataFrame([[20996.0, 20998.0, 20994.0, 20997.0, 100.0]],
                           index=[minute], columns=_cols)
        rowm = pd.DataFrame([[2990.0, 2992.0, 2988.0, 2991.0, 100.0]],
                            index=[minute], columns=_cols)
        pipeline.on_1m_bar(sec_ts, _bar(20996.0, high_off=2.0), _bar(2990.0, high_off=2.0),
                           row, rowm, bar_complete=False)

    assert calls["trend"] == n_secs, f"run_trend must fire every 1s call, got {calls['trend']}"
    assert calls["strategy"] == n_secs, f"run_strategy must fire every 1s call, got {calls['strategy']}"
