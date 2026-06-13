# tests/test_smt_warmup_startup.py
# Focused verification for the pre-startup SMT warm-up replay (session_pipeline).
#
# A late orchestrator start (after the 18:00 ET CME session open) must replay the
# pre-startup session bars [session_open, now) through the SMT detector so SMTs that
# fired before startup are still detected, logged with their REAL occurrence time, and
# folded into detect_state — instead of being silently missed.
#
# Ground truth (real data, 2026-06-08 CME session): an early BULLISH SMT forms at the
# session-open bar (2026-06-07 18:00 ET) and again at 18:56 ET against `week_low`, a
# level set on 2026-06-05 16:48 ET (price 28780.25). A start at 19:30 ET (1.5 h late)
# would otherwise miss both. The warm-up must catch them, tagged source="v2-warmup".

from __future__ import annotations

import pandas as pd
import pytest

import paths
import smt_state
from session_pipeline import SessionPipeline
from session_times import cme_session_start

_ET = "America/New_York"

# June-8 CME session reference + the late-start instant under test.
_SESSION_REF = pd.Timestamp("2026-06-08 12:00", tz=_ET)
_LATE_NOW = pd.Timestamp("2026-06-07 19:30", tz=_ET)  # 1.5 h after the 18:00 open

# Ground-truth bullish SMT (June-5-derived week_low). Established by replaying the
# session from open through the live detector (see report); week_low = 28780.25 was set
# 2026-06-05 16:48 ET. The wick at 18:00 dedups into the body; the 18:56 wick survives.
_GT_REF_NAME = "week_low"
_GT_WEEK_LOW = 28780.25
_GT_OCCURRENCE_TIMES = {
    "2026-06-07T18:00:00-04:00",
    "2026-06-07T18:56:00-04:00",
}


def _load_live_1m(sym: str) -> "pd.DataFrame | None":
    """Load the LIVE 1m parquet the orchestrator actually reads (via paths), or None."""
    p = paths.general_live_dir() / f"{sym}_1m.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    return df if isinstance(df.index, pd.DatetimeIndex) else None


def _june8_frames():
    """Return (mnq, mes) live 1m frames if both cover the June-8 session, else None."""
    mnq = _load_live_1m("MNQ")
    mes = _load_live_1m("MES")
    if mnq is None or mes is None:
        return None
    ss = pd.Timestamp(cme_session_start(_SESSION_REF))
    # Need pre-startup session bars [session_open, late_now) for both instruments.
    have_mnq = len(mnq[(mnq.index >= ss) & (mnq.index < _LATE_NOW)]) > 0
    have_mes = len(mes[(mes.index >= ss) & (mes.index < _LATE_NOW)]) > 0
    if not (have_mnq and have_mes):
        return None
    return mnq, mes


@pytest.fixture()
def _inmem_state(tmp_path, monkeypatch):
    """Fully isolate state: in-memory store for the four state JSONs PLUS a tmp `state_dir`
    so the un-gated `levels.json` / `smt_invalidations.json` writes (paths.state_dir())
    never touch the worktree's data/ dir or any live session folder.

    NOTE: only `_STATE_DIR` is redirected — NOT `ACT_GLOBAL_DIR`. `paths.state_dir()` keys
    off `_STATE_DIR` alone, while the test's June-8 bar data is resolved via
    `paths.general_live_dir()` (→ `ACT_GLOBAL_DIR`/global_root), which must stay pointed at
    the real live parquets. Reads of those parquets are non-mutating."""
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    smt_state.set_in_memory_mode(True)
    try:
        yield
    finally:
        smt_state.set_in_memory_mode(False)


def _build_late_start_pipeline(mnq: pd.DataFrame, mes: pd.DataFrame, now: pd.Timestamp):
    """Construct a pipeline exactly as automation.main's dispatcher does at a LATE start:
    full IB frames truncated at `now` (history + this session's bars so far), then
    on_session_start with today_at_open = [session_open, now]."""
    ss = pd.Timestamp(cme_session_start(now))
    mnq_full = mnq[mnq.index < now]
    mes_full = mes[mes.index < now]
    events: list[dict] = []
    pipe = SessionPipeline(mnq_full, mes_full, events.append)
    today_at_open = mnq_full[(mnq_full.index >= ss) & (mnq_full.index <= now)]
    pipe.on_session_start(now, today_at_open)
    return pipe, events


def test_warmup_catches_june8_bullish_smt(_inmem_state):
    """A late June-8 start replays [session_open, now) and catches the early bullish
    week_low (June-5-derived) SMT it would otherwise have missed, tagged v2-warmup."""
    frames = _june8_frames()
    if frames is None:
        pytest.skip("2026-06-08 live 1m data (MNQ+MES) not available via paths")
    mnq, mes = frames

    pipe, events = _build_late_start_pipeline(mnq, mes, _LATE_NOW)

    warmups = [e for e in events
               if e.get("kind") == "smt-div" and e.get("source") == "v2-warmup"]
    assert warmups, "late start produced no warm-up SMTs"

    # The ground-truth bullish week_low SMT must be among them.
    bull = [e for e in warmups
            if e.get("side") == "bullish" and e.get("ref_name") == _GT_REF_NAME]
    assert bull, f"bullish {_GT_REF_NAME} SMT not detected by warm-up; got {warmups}"

    # Same level (June-5-derived week_low) and direction.
    assert any(e.get("mnq_div_price") == _GT_WEEK_LOW for e in bull), \
        f"expected week_low {_GT_WEEK_LOW}; got {[e.get('mnq_div_price') for e in bull]}"

    # Occurrence time is the REAL pre-startup bar, not `now`, and lands at a known firing bar.
    bull_times = {e["time"] for e in bull}
    assert bull_times & _GT_OCCURRENCE_TIMES, \
        f"warm-up SMT times {bull_times} miss firing bars {_GT_OCCURRENCE_TIMES}"
    assert all(e["time"] != _LATE_NOW.isoformat() for e in bull), \
        "warm-up SMT time must be the occurrence bar, not the startup instant"

    # Warm-up marker contract: warmup flag + logged_at == startup now.
    for e in bull:
        assert e.get("warmup") is True
        assert e.get("logged_at") == _LATE_NOW.isoformat()

    # detect_state was populated by the warm-up (excluding reserved __bookkeeping__ keys).
    assert any(not str(k).startswith("__") for k in pipe._detect_state), \
        "warm-up did not populate detect_state"


def test_warmup_is_restart_safe(_inmem_state):
    """A warm restart (detect_state already reflects the pre-startup bars) must NOT
    replay them again — the cold-start guard skips the warm-up to avoid double-firing."""
    frames = _june8_frames()
    if frames is None:
        pytest.skip("2026-06-08 live 1m data (MNQ+MES) not available via paths")
    mnq, mes = frames

    # Cold start: warm-up fires and persists detect_state into the (in-memory) store.
    _pipe1, events1 = _build_late_start_pipeline(mnq, mes, _LATE_NOW)
    cold = [e for e in events1 if e.get("source") == "v2-warmup"]
    assert cold, "cold start should produce warm-up SMTs"

    # Warm restart against the same in-memory store: detect_state is restored non-empty,
    # so the guard must skip the replay entirely (no duplicate warm-up emissions).
    _pipe2, events2 = _build_late_start_pipeline(mnq, mes, _LATE_NOW)
    warm = [e for e in events2 if e.get("source") == "v2-warmup"]
    assert warm == [], f"warm restart re-fired {len(warm)} warm-up SMTs (double-fire)"


def test_warmup_skipped_when_started_at_session_open(_inmem_state):
    """No pre-startup gap (now == session open) → the warm-up window [open, now) is empty,
    so no warm-up SMTs are emitted (nothing was missed)."""
    frames = _june8_frames()
    if frames is None:
        pytest.skip("2026-06-08 live 1m data (MNQ+MES) not available via paths")
    mnq, mes = frames

    ss = pd.Timestamp(cme_session_start(_SESSION_REF))
    _pipe, events = _build_late_start_pipeline(mnq, mes, ss)
    warmups = [e for e in events if e.get("source") == "v2-warmup"]
    assert warmups == [], f"on-time start should emit no warm-up SMTs; got {warmups}"
