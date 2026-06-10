# tests/test_gap_fill.py
# Unit tests for gap_fill.gap_fill_until_now and IbRealtimeSource.gap_fill().

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import gap_fill


# ---------------------------------------------------------------------------
# gap_fill_until_now
# ---------------------------------------------------------------------------

def test_gap_fill_until_now_runs_reachable_merge_and_source(monkeypatch, tmp_path):
    """Happy path: reachable check + session merge + fill-only IbRealtimeSource.gap_fill()."""
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

    gap_fill.gap_fill_until_now(tmp_path)

    reachable.assert_called_once()
    merge.assert_called_once_with(tmp_path)
    assert len(instances) == 1
    src = instances[0]
    src.gap_fill.assert_called_once()
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


def test_source_gap_fill_runs_1s_then_1m(monkeypatch, tmp_path):
    """gap_fill(): load → 1s fill → 1m fill → reload, in order."""
    src = _make_source(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(src, "_load_parquets", lambda: calls.append("load"))
    monkeypatch.setattr(src, "_gap_fill_1s_ib", lambda: calls.append("1s") or True)
    import data.ib_realtime as _ir
    monkeypatch.setattr(_ir, "gap_fill_1m_ib", lambda d: calls.append(f"1m:{d}"))

    src.gap_fill()

    assert calls == ["load", "1s", f"1m:{tmp_path}", "load"]


def test_source_gap_fill_raises_when_1s_fails(monkeypatch, tmp_path):
    """1s fill returning False raises and blocks the 1m fill."""
    src = _make_source(tmp_path)
    monkeypatch.setattr(src, "_load_parquets", lambda: None)
    monkeypatch.setattr(src, "_gap_fill_1s_ib", lambda: False)
    one_m = MagicMock()
    import data.ib_realtime as _ir
    monkeypatch.setattr(_ir, "gap_fill_1m_ib", one_m)

    with pytest.raises(RuntimeError):
        src.gap_fill()
    one_m.assert_not_called()
