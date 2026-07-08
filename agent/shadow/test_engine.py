"""ShadowEngine tests (Wave 2.3) — all offline via the StubBackend."""

import os
import sys

import pandas as pd
import pytest

from conftest import DOCS_ROOT, fixtures_present, load_live_frames

import engine as engine_mod
from engine import ShadowEngine
from facts_adapter import Snapshot
from records import read_audit_records
from run_agent import StubBackend
from shadow_config import ShadowConfig

pytestmark = pytest.mark.skipif(not fixtures_present(), reason="golden fixtures absent")

_HYP = {"kind": "new-hypothesis", "time": "2026-05-19 09:27:59-04:00", "direction": "down",
        "price": 29200.0, "cautious_price_initial_level": "prev1_day_low"}


def _make_engine(tmp_path, backend=None, churn_guard=False):
    config = ShadowConfig(real_api=False, churn_guard=churn_guard, latency_sec=79.0,
                          cache_dir=str(tmp_path / "cache"))
    return ShadowEngine(config, backend or StubBackend(), DOCS_ROOT,
                        out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"))


def test_checkpoint_then_trigger_carries_both(tmp_path):
    eng = _make_engine(tmp_path)
    frames = load_live_frames()
    eng.on_checkpoint(frames["now"], frames)
    assert eng.standing_daily_trend is not None

    rec = eng.on_hypothesis_trigger(frames["now"], frames, _HYP, "5m_boundary")
    assert rec.decision["daily_trend"] == eng.standing_daily_trend
    assert "next_move" in rec.decision
    assert rec.paired_diff["direction"]["hypothesis"] == "down"
    assert read_audit_records(eng.audit_path)              # audit written


def test_trigger_before_checkpoint_uses_neutral_default(tmp_path):
    eng = _make_engine(tmp_path)
    frames = load_live_frames()
    rec = eng.on_hypothesis_trigger(frames["now"], frames, _HYP, "session_start")
    assert rec.decision["daily_trend"]["direction"] == "neutral"   # neutral default
    assert rec.standing_daily_trend["confidence"] == "low"


def test_guard_kill_path_no_api_call(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    frames = load_live_frames()

    def _poisoned(frames, checkpoint=None):
        now = checkpoint if checkpoint is not None else frames["now"]
        return Snapshot(text="## S0", validator_dict={"now_price": 1.0},
                        content_hash="poison", max_ts=now + pd.Timedelta(seconds=1),
                        now=now, checkpoint=checkpoint)

    monkeypatch.setattr(engine_mod, "build_snapshot", _poisoned)
    rec = eng.on_hypothesis_trigger(frames["now"], frames, _HYP, "5m_boundary")
    assert rec.error == "lookahead"
    assert rec.verdict == "guard-kill"
    assert rec.fallback is True
    assert eng.stats["api_calls"] == 0                     # decision discarded, no call
    assert eng.stats["guard_kills"] == 1


def _invalid_next():
    # direction up with no move_target → SYN_DIRECTIONAL_MISSING_TARGET (never validates).
    return {"direction": "up", "confidence": "high", "move_target": None,
            "flip_trigger": "x", "flipped_target": None, "arm_entry_confirmation": "yes",
            "resolution": {"long_if": {"condition": "x", "price": None, "target": None},
                           "short_if": {"condition": "y", "price": None, "target": None}},
            "bull_ledger": [], "bear_ledger": [], "N": 0.0, "vetoes": [],
            "reasoning": "invalid"}


def test_failsafe_path(tmp_path):
    backend = StubBackend(responses=[_invalid_next(), _invalid_next(), _invalid_next()])
    eng = _make_engine(tmp_path, backend=backend)
    frames = load_live_frames()
    rec = eng.on_hypothesis_trigger(frames["now"], frames, _HYP, "5m_boundary")
    assert rec.verdict == "failsafe"
    assert rec.fallback is True
    assert rec.decision["next_move"]["direction"] == "neutral"   # NEUTRAL/LOW failsafe
    assert rec.decision["next_move"]["confidence"] == "low"


def test_churn_guard_suppresses_second_trigger(tmp_path):
    eng = _make_engine(tmp_path, churn_guard=True)
    frames = load_live_frames()
    eng.on_hypothesis_trigger(frames["now"], frames, _HYP, "5m_boundary")
    calls_after_first = eng.stats["api_calls"]
    rec2 = eng.on_hypothesis_trigger(frames["now"], frames, _HYP, "5m_boundary")
    assert rec2.verdict == "churn-suppressed"
    assert eng.stats["api_calls"] == calls_after_first     # no new API call
