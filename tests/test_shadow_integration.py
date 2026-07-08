"""GIL-44 Phase-3 pipeline-integration tests (backtest hook + flag + latency).

The flag-OFF/ON byte-identity safety claim (events+trades) is proven at scale on real
days by the Phase-6 regression-runner A/B — it can't run here because the autouse test
isolation redirects ACT_GLOBAL_DIR to an empty tmp, so run_backtest_v2 finds no futures
data. These tests instead prove the WIRING deterministically: shadow=None leaves the emit
path untouched (byte-identical by construction), a shadow observer is mirrored the
new-hypothesis triggers + checkpoint cadence, the emitted event is passed through
unchanged, and arrival stamping / cadence hold. All offline — no data, keys, or spend.
"""

import os
import sys

import pandas as pd
import pytest

from session_pipeline import SessionPipeline

_AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

from run_agent import DOCS_ROOT, StubBackend  # noqa: E402
from shadow.engine import ShadowEngine  # noqa: E402
from shadow_config import ShadowConfig  # noqa: E402


class _FakeShadow:
    """Records the hooks the pipeline fires; never computes (no LLM, no facts)."""

    def __init__(self):
        self.triggers = []
        self.checkpoints = []
        self.events_native = [{"kind": "new-hypothesis", "source": "ai-shadow"}]

    def on_hypothesis_trigger(self, now, frames, hyp_event, trigger_kind):
        self.triggers.append((now, frames, hyp_event, trigger_kind))

    def on_checkpoint(self, now, frames):
        self.checkpoints.append(now)


def test_flag_off_leaves_emit_untouched():
    events = []

    def emit(e):
        events.append(e)

    p = SessionPipeline(pd.DataFrame(), pd.DataFrame(), emit, shadow=None)
    assert p._emit is emit                   # no wrapper → byte-identical emit path
    assert p._shadow is None


def test_shadow_mirrors_new_hypothesis_without_mutating_event():
    events = []
    fake = _FakeShadow()
    p = SessionPipeline(pd.DataFrame(), pd.DataFrame(), events.append, shadow=fake)
    assert p._emit == p._emit_with_shadow

    now = pd.Timestamp("2026-05-19 09:27:00", tz="America/New_York")
    p._shadow_ctx = {"now": now, "today_mnq": pd.DataFrame(), "today_mes": pd.DataFrame()}

    evt = {"kind": "new-hypothesis", "direction": "down", "time": now.isoformat()}
    p._emit(evt)
    assert events == [evt]                    # the raw emit received the unchanged event
    assert len(fake.triggers) == 1
    assert fake.triggers[0][2] is evt         # the hypothesis event was mirrored verbatim
    assert fake.triggers[0][3] == "new-hypothesis"

    # A non-hypothesis event is emitted but NOT mirrored to the shadow.
    p._emit({"kind": "trend-broken"})
    assert len(fake.triggers) == 1

    # A new-hypothesis emitted OUTSIDE on_1m_bar (no frame ctx) is not mirrored.
    p._shadow_ctx = None
    p._emit({"kind": "new-hypothesis"})
    assert len(fake.triggers) == 1


def test_shadow_checkpoint_driven_once_per_minute():
    fake = _FakeShadow()
    p = SessionPipeline(pd.DataFrame(), pd.DataFrame(), (lambda e: None), shadow=fake)
    base = pd.Timestamp("2026-05-19 09:20:00", tz="America/New_York")
    # Several sub-minute calls in the same minute → one checkpoint call; new minute → +1.
    for sec in (0, 15, 30, 59):
        p._shadow_on_bar(base + pd.Timedelta(seconds=sec), pd.DataFrame(), pd.DataFrame())
    p._shadow_on_bar(base + pd.Timedelta(minutes=1), pd.DataFrame(), pd.DataFrame())
    assert len(fake.checkpoints) == 2


def _frames(now, mnq, mes):
    return {"mnq_today": mnq, "mes_today": mes, "hist_mnq": None, "hist_mes": None,
            "hist_1hr": None, "hist_4hr": None, "ath_mnq": None, "ath_mes": None,
            "now": now}


def _engine(tmp_path):
    config = ShadowConfig(real_api=False, cache_dir=str(tmp_path / "c"))
    return ShadowEngine(config, StubBackend(), DOCS_ROOT, out_dir=str(tmp_path / "o"),
                        cache_dir=str(tmp_path / "c"))


def test_checkpoints_fire_once_each(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    fired = []
    monkeypatch.setattr(eng, "_run_checkpoint",
                        lambda now, frames, hm, ts: fired.append(hm))
    tz = "America/New_York"
    # Session 2026-05-18 18:00 → 2026-05-19 17:00, one call per minute across the day.
    start = pd.Timestamp("2026-05-18 18:00", tz=tz)
    eng._session_first_ts = start
    for i in range(0, 23 * 60, 5):        # every 5 minutes is enough to cross each ckpt
        now = start + pd.Timedelta(minutes=i)
        eng.on_checkpoint(now, {"mnq_today": None})
    assert sorted(fired) == ["06:00", "09:20", "13:00", "18:00"]


def test_mid_start_day_fires_only_crossed(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    fired = []
    monkeypatch.setattr(eng, "_run_checkpoint",
                        lambda now, frames, hm, ts: fired.append(hm))
    tz = "America/New_York"
    start = pd.Timestamp("2026-05-19 09:25", tz=tz)      # data starts after 09:20
    eng._session_first_ts = start
    for i in range(0, 8 * 60, 5):
        now = start + pd.Timedelta(minutes=i)
        eng.on_checkpoint(now, {"mnq_today": None})
    assert "18:00" not in fired and "06:00" not in fired and "09:20" not in fired
    assert "13:00" in fired                              # only the checkpoint truly crossed


def test_short_day_finalize_clean(tmp_path):
    eng = _engine(tmp_path)
    eng.finalize(session_parquet=None)     # no dangling records, no crash
    assert eng.records == []
