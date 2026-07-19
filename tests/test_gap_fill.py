# tests/test_gap_fill.py
# Unit tests for gap_fill.gap_fill_until_now and IbRealtimeSource.gap_fill().

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

import gap_fill


# ---------------------------------------------------------------------------
# gap_fill_until_now
# ---------------------------------------------------------------------------

def test_gap_fill_until_now_runs_reachable_merge_and_source(monkeypatch, tmp_path):
    """Happy path: reachable check + session merge + fill-only IbRealtimeSource.gap_fill(),
    run through run_gap_fill_with_retries()."""
    monkeypatch.setenv("MNQ_CONID", "111")
    monkeypatch.setenv("MES_CONID", "222")
    monkeypatch.setenv("IB_HOST", "1.2.3.4")
    monkeypatch.setenv("IB_PORT", "4321")
    monkeypatch.setenv("PRE_SESSION_IB_CLIENT_ID", "10")

    reachable = MagicMock()
    monkeypatch.setattr(gap_fill, "check_ib_reachable", reachable)

    merge = MagicMock()
    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "merge_session_1s_parquets", merge)

    instances: list = []
    import data.ib_realtime as _ir

    class _FakeSource:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.gap_fill = MagicMock()
            instances.append(self)

    monkeypatch.setattr(_ir, "IbRealtimeSource", _FakeSource)

    retries_calls: list = []
    monkeypatch.setattr(
        _ir, "run_gap_fill_with_retries",
        lambda fn, d, **kw: retries_calls.append((fn, d)) or fn(),
    )

    gap_fill.gap_fill_until_now(tmp_path)

    reachable.assert_called_once()
    merge.assert_called_once_with(tmp_path)
    assert len(instances) == 1
    src = instances[0]
    src.gap_fill.assert_called_once()
    assert retries_calls == [(src.gap_fill, tmp_path)]
    assert src.kwargs["host"] == "1.2.3.4"
    assert src.kwargs["port"] == 4321
    assert src.kwargs["client_id"] == 10
    assert src.kwargs["mnq_conid"] == "111"
    assert src.kwargs["mes_conid"] == "222"
    assert src.kwargs["bar_data_dir"] == tmp_path


def test_gap_fill_until_now_skips_when_conids_absent(monkeypatch, tmp_path, capsys):
    """No MNQ/MES conids → no source constructed; reachable + merge still run."""
    monkeypatch.delenv("MNQ_CONID", raising=False)
    monkeypatch.delenv("MES_CONID", raising=False)

    reachable = MagicMock()
    monkeypatch.setattr(gap_fill, "check_ib_reachable", reachable)
    merge = MagicMock()
    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "merge_session_1s_parquets", merge)

    constructed: list = []
    import data.ib_realtime as _ir
    monkeypatch.setattr(_ir, "IbRealtimeSource",
                        lambda **k: constructed.append(k))

    gap_fill.gap_fill_until_now(tmp_path)

    reachable.assert_called_once()
    merge.assert_called_once_with(tmp_path)
    assert constructed == []
    assert "cannot gap-fill" in capsys.readouterr().out


def test_gap_fill_until_now_honours_skip_flags(monkeypatch, tmp_path):
    """check_reachable=False / merge_sessions=False skip those steps."""
    monkeypatch.setenv("MNQ_CONID", "1")
    monkeypatch.setenv("MES_CONID", "2")

    reachable = MagicMock()
    monkeypatch.setattr(gap_fill, "check_ib_reachable", reachable)
    merge = MagicMock()
    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "merge_session_1s_parquets", merge)

    import data.ib_realtime as _ir
    fake = MagicMock()
    monkeypatch.setattr(_ir, "IbRealtimeSource", lambda **k: fake)
    monkeypatch.setattr(
        _ir, "run_gap_fill_with_retries",
        lambda fn, d, **kw: fn(),
    )

    gap_fill.gap_fill_until_now(tmp_path, check_reachable=False, merge_sessions=False)

    reachable.assert_not_called()
    merge.assert_not_called()
    fake.gap_fill.assert_called_once()


def test_gap_fill_until_now_defaults_dir_to_general_live(monkeypatch):
    """bar_data_dir omitted → resolves via paths.general_live_dir()."""
    import paths
    sentinel = object()
    monkeypatch.setattr(paths, "general_live_dir", lambda: sentinel)
    monkeypatch.delenv("MNQ_CONID", raising=False)
    monkeypatch.delenv("MES_CONID", raising=False)
    monkeypatch.setattr(gap_fill, "check_ib_reachable", MagicMock())
    captured = {}
    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "merge_session_1s_parquets",
                        lambda d: captured.setdefault("dir", d))

    gap_fill.gap_fill_until_now()

    assert captured["dir"] is sentinel


# ---------------------------------------------------------------------------
# IbRealtimeSource.gap_fill()
# ---------------------------------------------------------------------------

def _make_source(tmp_path):
    from data.ib_realtime import IbRealtimeSource
    return IbRealtimeSource(
        host="127.0.0.1", port=4002, client_id=10,
        mnq_conid="1", mes_conid="2",
        bar_data_dir=tmp_path, on_bar=lambda *_: None,
    )


def test_source_gap_fill_runs_1m_then_1s(monkeypatch, tmp_path):
    """gap_fill(): load → 1m fill → reload → 1s fill, in order.

    1m runs FIRST: it is cheap (few requests) and is what signals depend on, so it
    must not compete with a pacing budget the 1s fill has already exhausted."""
    src = _make_source(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(src, "_load_parquets", lambda: calls.append("load"))
    monkeypatch.setattr(src, "_gap_fill_1s_ib", lambda: calls.append("1s") or True)
    import data.ib_realtime as _ir
    monkeypatch.setattr(_ir, "gap_fill_1m_ib", lambda d: calls.append(f"1m:{d}"))

    src.gap_fill()

    assert calls == ["load", f"1m:{tmp_path}", "load", "1s"]


def test_source_gap_fill_degrades_when_1s_fails(monkeypatch, tmp_path):
    """An incomplete 1s fill must NOT raise — it WARNs and still runs the 1m fill.

    Graceful degradation so automation.main stays alive (like the orchestrator) instead of
    crashing on IB pacing / no-data over a large gap; realtime 1s fills forward and the
    session-end parquet-check backfills the residual historical 1s gap.
    """
    src = _make_source(tmp_path)
    monkeypatch.setattr(src, "_load_parquets", lambda: None)
    monkeypatch.setattr(src, "_gap_fill_1s_ib", lambda: False)
    one_m = MagicMock()
    import data.ib_realtime as _ir
    monkeypatch.setattr(_ir, "gap_fill_1m_ib", one_m)

    # Must not raise, and the 1m fill must still run despite the 1s shortfall.
    src.gap_fill()
    one_m.assert_called_once()


# ---------------------------------------------------------------------------
# gap_hours_by_file
# ---------------------------------------------------------------------------

def _write_bar(path, ts):
    df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([ts]),
    )
    df.to_parquet(path)


def test_gap_hours_by_file_all_current(tmp_path):
    from data.ib_realtime import gap_hours_by_file
    now = pd.Timestamp.now(tz="America/New_York")
    for name in ("MNQ_1s.parquet", "MES_1s.parquet", "MNQ_1m.parquet", "MES_1m.parquet"):
        _write_bar(tmp_path / name, now)

    gaps = gap_hours_by_file(tmp_path)

    assert set(gaps) == {"MNQ_1s.parquet", "MES_1s.parquet", "MNQ_1m.parquet", "MES_1m.parquet"}
    assert all(0.0 <= h < 0.01 for h in gaps.values())


def test_gap_hours_by_file_missing_file_is_inf(tmp_path):
    from data.ib_realtime import gap_hours_by_file
    now = pd.Timestamp.now(tz="America/New_York")
    _write_bar(tmp_path / "MNQ_1s.parquet", now)
    # MES_1s.parquet, MNQ_1m.parquet, MES_1m.parquet intentionally absent

    gaps = gap_hours_by_file(tmp_path)

    assert gaps["MNQ_1s.parquet"] < 0.01
    assert gaps["MES_1s.parquet"] == float("inf")
    assert gaps["MNQ_1m.parquet"] == float("inf")
    assert gaps["MES_1m.parquet"] == float("inf")


def test_gap_hours_by_file_empty_df_is_inf(tmp_path):
    from data.ib_realtime import gap_hours_by_file
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([]),
    )
    empty.to_parquet(tmp_path / "MNQ_1s.parquet")

    gaps = gap_hours_by_file(tmp_path)

    assert gaps["MNQ_1s.parquet"] == float("inf")


def test_gap_hours_by_file_weekend_clamps_to_friday_close(tmp_path):
    """On a weekend the newest fillable bar is Friday 17:00 ET — a parquet ending at
    Friday close must read as caught-up, or the retry loop can never converge."""
    from data.ib_realtime import gap_hours_by_file
    fri_close = pd.Timestamp("2026-06-12 16:59:59", tz="America/New_York")
    for name in ("MNQ_1s.parquet", "MES_1s.parquet", "MNQ_1m.parquet", "MES_1m.parquet"):
        _write_bar(tmp_path / name, fri_close)

    gaps = gap_hours_by_file(tmp_path, now=pd.Timestamp("2026-06-14 12:00", tz="America/New_York"))

    assert all(h < 1.0 for h in gaps.values()), gaps


def test_gap_hours_by_file_open_market_unclamped(tmp_path):
    """During trading hours the gap is measured against real now (unchanged behavior)."""
    from data.ib_realtime import gap_hours_by_file
    _write_bar(tmp_path / "MNQ_1s.parquet", pd.Timestamp("2026-06-16 10:00", tz="America/New_York"))

    gaps = gap_hours_by_file(tmp_path, now=pd.Timestamp("2026-06-16 12:00", tz="America/New_York"))

    assert abs(gaps["MNQ_1s.parquet"] - 2.0) < 0.01


# ---------------------------------------------------------------------------
# GapFillFailedError
# ---------------------------------------------------------------------------

def test_gap_fill_failed_error_carries_gaps_and_last_error():
    from data.ib_realtime import GapFillFailedError
    last_error = RuntimeError("ib broke")
    err = GapFillFailedError({"MNQ_1s.parquet": 50.0}, last_error)

    assert err.gaps_hours == {"MNQ_1s.parquet": 50.0}
    assert err.last_error is last_error
    assert "50.0" in str(err) or "50" in str(err)


# ---------------------------------------------------------------------------
# run_gap_fill_with_retries
# ---------------------------------------------------------------------------

def test_run_gap_fill_with_retries_single_round_closes_gap(monkeypatch, tmp_path):
    import data.ib_realtime as _ir

    gap_sequence = [{"MNQ_1s.parquet": 2.0}, {"MNQ_1s.parquet": 0.1}]
    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: gap_sequence.pop(0))
    sleep_mock = MagicMock()
    monkeypatch.setattr(_ir.time, "sleep", sleep_mock)

    round_calls = []
    _ir.run_gap_fill_with_retries(lambda: round_calls.append(1), tmp_path)

    assert round_calls == [1]
    sleep_mock.assert_not_called()


def test_run_gap_fill_with_retries_multiple_rounds_until_caught_up(monkeypatch, tmp_path, capsys):
    import data.ib_realtime as _ir

    gap_sequence = [
        {"MES_1s.parquet": 100.0},  # headsup check (>24h -> message printed)
        {"MES_1s.parquet": 50.0},   # after round 1 (still >1h)
        {"MES_1s.parquet": 20.0},   # after round 2 (still >1h)
        {"MES_1s.parquet": 0.5},    # after round 3 (<=1h -> done)
    ]
    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: gap_sequence.pop(0))
    sleep_mock = MagicMock()
    monkeypatch.setattr(_ir.time, "sleep", sleep_mock)

    round_calls = []
    _ir.run_gap_fill_with_retries(
        lambda: round_calls.append(1), tmp_path, round_spacing_s=650.0
    )

    assert round_calls == [1, 1, 1]
    assert sleep_mock.call_count == 2
    sleep_mock.assert_called_with(650.0)
    assert "Large gap detected" in capsys.readouterr().out


def test_run_gap_fill_with_retries_no_headsup_for_small_initial_gap(monkeypatch, tmp_path, capsys):
    import data.ib_realtime as _ir

    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: {"MNQ_1s.parquet": 0.1})
    monkeypatch.setattr(_ir.time, "sleep", MagicMock())

    _ir.run_gap_fill_with_retries(lambda: None, tmp_path)

    assert "Large gap detected" not in capsys.readouterr().out


def test_run_gap_fill_with_retries_failure_counter_resets_on_success(monkeypatch, tmp_path):
    import data.ib_realtime as _ir

    # 7 rounds: fail, ok (resets counter), then 5 more consecutive fails -> raises on round 7.
    # If the counter did NOT reset after "ok", it would raise on round 6 instead (only 5
    # total fails needed cumulatively) -- asserting call_idx == 7 catches that regression.
    call_sequence = ["fail", "ok", "fail", "fail", "fail", "fail", "fail"]
    call_idx = [0]

    def _do_round():
        outcome = call_sequence[call_idx[0]]
        call_idx[0] += 1
        if outcome == "fail":
            raise RuntimeError("boom")

    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: {"MNQ_1s.parquet": 100.0})
    monkeypatch.setattr(_ir.time, "sleep", MagicMock())

    with pytest.raises(_ir.GapFillFailedError):
        _ir.run_gap_fill_with_retries(_do_round, tmp_path, max_consecutive_failures=5)

    assert call_idx[0] == 7


def test_run_gap_fill_with_retries_raises_after_max_consecutive_failures(monkeypatch, tmp_path):
    import data.ib_realtime as _ir

    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: {"MNQ_1s.parquet": 50.0})
    monkeypatch.setattr(_ir.time, "sleep", MagicMock())

    def _always_fail():
        raise RuntimeError("ib broke")

    with pytest.raises(_ir.GapFillFailedError) as exc_info:
        _ir.run_gap_fill_with_retries(_always_fail, tmp_path, max_consecutive_failures=5)

    err = exc_info.value
    assert err.gaps_hours == {"MNQ_1s.parquet": 50.0}
    assert isinstance(err.last_error, RuntimeError)
    assert str(err.last_error) == "ib broke"


# ---------------------------------------------------------------------------
# interior 1s gap detection + repair (offline path)
# ---------------------------------------------------------------------------

def _bars_df(timestamps):
    n = len(timestamps)
    return pd.DataFrame(
        {"Open": [1.0] * n, "High": [1.0] * n, "Low": [1.0] * n,
         "Close": [1.0] * n, "Volume": [1.0] * n},
        index=pd.DatetimeIndex(timestamps),
    )


def test_find_interior_1s_gaps_detects_open_market_hole():
    """A 30-min hole on a Tuesday morning is a repairable interior gap."""
    ET = "America/New_York"
    idx = list(pd.date_range("2026-06-16 09:00", "2026-06-16 10:00", freq="1s", tz=ET))
    idx += list(pd.date_range("2026-06-16 10:30", "2026-06-16 11:00", freq="1s", tz=ET))
    gaps = gap_fill.find_interior_1s_gaps(_bars_df(idx))

    assert len(gaps) == 1
    start, end = gaps[0]
    assert start == pd.Timestamp("2026-06-16 10:00", tz=ET)
    assert end == pd.Timestamp("2026-06-16 10:30", tz=ET)


def test_find_interior_1s_gaps_ignores_expected_closures():
    """Maintenance-break, weekend, and holiday-early-close gaps are not repairable holes."""
    ET = "America/New_York"
    # Daily maintenance break Tue 17:00-18:00.
    idx = list(pd.date_range("2026-06-16 16:30", "2026-06-16 16:59:59", freq="1s", tz=ET))
    idx += list(pd.date_range("2026-06-16 18:00", "2026-06-16 18:30", freq="1s", tz=ET))
    assert gap_fill.find_interior_1s_gaps(_bars_df(idx)) == []
    # Juneteenth 13:00 early close straight into the Sunday 18:00 reopen.
    idx = list(pd.date_range("2026-06-19 12:30", "2026-06-19 12:59:59", freq="1s", tz=ET))
    idx += list(pd.date_range("2026-06-21 18:00", "2026-06-21 18:10", freq="1s", tz=ET))
    assert gap_fill.find_interior_1s_gaps(_bars_df(idx)) == []


def test_repair_interior_1s_gaps_merges_fetched_bars(monkeypatch, tmp_path):
    """Detected holes are fetched via _fetch_gap_chunked and merged atomically."""
    ET = "America/New_York"
    idx = list(pd.date_range("2026-06-16 09:00", "2026-06-16 10:00", freq="1s", tz=ET))
    idx += list(pd.date_range("2026-06-16 10:30", "2026-06-16 11:00", freq="1s", tz=ET))
    df = _bars_df(idx)
    df.to_parquet(tmp_path / "MNQ_1s.parquet")
    _bars_df(list(pd.date_range("2026-06-16 09:00", "2026-06-16 11:00", freq="1s", tz=ET))
             ).to_parquet(tmp_path / "MES_1s.parquet")  # clean — no fetch expected

    monkeypatch.setenv("MNQ_CONID", "111")
    monkeypatch.setenv("MES_CONID", "222")

    fake_ib = MagicMock()
    fake_ib.isConnected.return_value = True
    monkeypatch.setattr(gap_fill, "_connect_ib", lambda: fake_ib)

    fetch_calls = []
    hole = _bars_df(list(pd.date_range("2026-06-16 10:00:01", "2026-06-16 10:29:59", freq="1s", tz=ET)))

    def _fake_fetch(ib, contract, gap_start, gap_end):
        fetch_calls.append((gap_start, gap_end))
        return hole, True

    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "_fetch_gap_chunked", _fake_fetch)

    gap_fill.repair_interior_1s_gaps(tmp_path)

    assert len(fetch_calls) == 1  # only MNQ's hole fetched
    repaired = pd.read_parquet(tmp_path / "MNQ_1s.parquet")
    assert len(repaired) == len(df) + len(hole)
    assert repaired.index.is_monotonic_increasing


def test_repair_interior_1s_gaps_clean_files_no_ib_connection(monkeypatch, tmp_path):
    """No repairable holes → never connects to IB."""
    ET = "America/New_York"
    _bars_df(list(pd.date_range("2026-06-16 09:00", "2026-06-16 10:00", freq="1s", tz=ET))
             ).to_parquet(tmp_path / "MNQ_1s.parquet")
    monkeypatch.setenv("MNQ_CONID", "111")
    monkeypatch.setenv("MES_CONID", "222")
    connect = MagicMock()
    monkeypatch.setattr(gap_fill, "_connect_ib", connect)

    gap_fill.repair_interior_1s_gaps(tmp_path)

    connect.assert_not_called()


def test_gap_fill_until_now_runs_interior_repair_after_retry_loop(monkeypatch, tmp_path):
    """The offline flow repairs interior holes after the forward fill completes."""
    monkeypatch.setenv("MNQ_CONID", "1")
    monkeypatch.setenv("MES_CONID", "2")
    monkeypatch.setattr(gap_fill, "check_ib_reachable", MagicMock())
    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "merge_session_1s_parquets", MagicMock())
    import data.ib_realtime as _ir
    order = []
    monkeypatch.setattr(_ir, "IbRealtimeSource", lambda **k: MagicMock())
    monkeypatch.setattr(_ir, "run_gap_fill_with_retries",
                        lambda fn, d, **kw: order.append("retries"))
    monkeypatch.setattr(gap_fill, "repair_interior_1s_gaps",
                        lambda d: order.append(f"repair:{d}"))

    gap_fill.gap_fill_until_now(tmp_path)

    assert order == ["retries", f"repair:{tmp_path}"]
