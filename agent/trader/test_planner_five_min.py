"""The §4/§5 suspension (2026-09-16). See `planner.five_min_armed` for the measurement."""
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.facts.detectors.legs import segment_legs
from agent.trader.planner import (FIVE_MIN_CLASSES, FIVE_MIN_ENV_FLAG,
                                  SELF_GATING_CLASSES, derive_plan, five_min_armed)

TZ = "America/New_York"


@pytest.fixture
def tape():
    """A clean DOWN leg into the arm, so §3 has a last trend to key the choice off."""
    idx = pd.date_range("2026-09-01 08:20", periods=60, freq="1min", tz=TZ)
    close = pd.Series(range(len(idx)), index=idx).astype(float) * -5.0 + 29500.0
    frame = pd.DataFrame({"Open": close + 1.0, "High": close + 2.0,
                          "Low": close - 2.0, "Close": close}, index=idx)
    now = idx[-1]
    return segment_legs(frame, now, "MNQ"), now


def _plan(tape, bias, **kw):
    legs, now = tape
    return derive_plan({"thesis_id": "t", "bias": bias,
                        "dol": {"level": "x", "price": 29000.0},
                        "falsified_if": []}, legs, now, **kw)


def test_neither_5m_class_is_armed_by_default(tape, monkeypatch):
    monkeypatch.delenv(FIVE_MIN_ENV_FLAG, raising=False)
    for bias in ("DOWN", "UP"):
        armed = _plan(tape, bias)["armed_classes"]
        assert not [c for c in armed if c in FIVE_MIN_CLASSES], armed
        # The suspension must not disturb the self-gating four.
        assert list(armed) == list(SELF_GATING_CLASSES)


def test_last_trend_is_still_recorded_while_suspended(tape, monkeypatch):
    """§3's reading is the plan's record of what the session was doing at the arm; it is
    not the 5m mechanisms' private state and must survive their suspension."""
    monkeypatch.delenv(FIVE_MIN_ENV_FLAG, raising=False)
    plan = _plan(tape, "DOWN")
    assert plan["last_trend"] is not None
    assert plan["last_trend"]["direction"] == "down"


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_the_env_flag_re_arms_them_and_the_rule_is_unchanged(tape, monkeypatch, raw):
    monkeypatch.setenv(FIVE_MIN_ENV_FLAG, raw)
    assert five_min_armed() is True
    # Thesis OPPOSES a down trend -> negation; AGREES -> continuation. §3, verbatim.
    assert _plan(tape, "UP")["armed_classes"][0] == "fvg_negation_reversal"
    assert _plan(tape, "DOWN")["armed_classes"][0] == "fvg_return_continuation"


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "nonsense"])
def test_only_an_explicit_affirmative_re_arms(tape, monkeypatch, raw):
    monkeypatch.setenv(FIVE_MIN_ENV_FLAG, raw)
    assert five_min_armed() is False
    assert not [c for c in _plan(tape, "DOWN")["armed_classes"] if c in FIVE_MIN_CLASSES]


def test_the_five_min_override_beats_the_flag_in_both_directions(tape, monkeypatch):
    """`agent/study/entries.py` depends on this: its subject IS the 5m entry path."""
    monkeypatch.delenv(FIVE_MIN_ENV_FLAG, raising=False)
    assert _plan(tape, "DOWN", five_min=True)["armed_classes"][0] == "fvg_return_continuation"
    monkeypatch.setenv(FIVE_MIN_ENV_FLAG, "1")
    assert not [c for c in _plan(tape, "DOWN", five_min=False)["armed_classes"]
                if c in FIVE_MIN_CLASSES]


def test_the_mechanisms_themselves_are_untouched():
    """Suspended, not removed: the Executor's resting path and the arbiter still know
    both names, so re-arming is one variable and never a revert."""
    from agent.trader.arbiter import RESTING_MECHANISMS
    from agent.trader.planner import MECHANISM_CLASSES
    for name in FIVE_MIN_CLASSES:
        assert name in MECHANISM_CLASSES
        assert name in RESTING_MECHANISMS
