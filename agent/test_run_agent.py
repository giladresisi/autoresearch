"""Tests for the Phase-2 decision-agent runner (GIL-44).

NO real API calls anywhere — every case drives the runner through a FakeBackend
injected via the Backend interface. Cut inputs are copied into a tmp folder so the
real calibration/cuts/ contents are never touched (the runner writes decision.json
next to the facts it reads).
"""

import copy
import json
import os
import shutil

import pytest

import run_agent
from run_agent import (
    AnthropicBackend,
    Backend,
    CallResponse,
    OpenRouterBackend,
    build_system_prompt,
    load_env_file,
    make_backend,
    run_cut,
)
from validator import DRIVER_WEIGHTS

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SRC_CUT = os.path.join(REPO_ROOT, "calibration", "cuts", "2026-05-19_0928")


# --------------------------------------------------------------------------- #
# Fixtures — clean / invalid decision blocks (NEUTRAL avoids semantic target   #
# checks; resolution prices are null so the wrong-side check is skipped).      #
# --------------------------------------------------------------------------- #
def _valid_daily() -> dict:
    return {
        "direction": "neutral",
        "confidence": "low",
        "regime": "range",
        "day_dol": None,
        "weakens_to_neutral_if": "close back above the daily mid",
        "flips_if": "sustained acceptance above prev-day high",
        "drivers": [
            {"id": did, "vote": 0, "weight": w, "contribution": 0.0}
            for did, w in DRIVER_WEIGHTS.items()
        ],
        "S": 0.0,
        "reasoning": "balanced — no directional edge at the checkpoint",
    }


def _invalid_daily() -> dict:
    """Seed the run-4 fractional-vote violation (vote 0.5 is not an integer)."""
    d = _valid_daily()
    d["drivers"][0]["vote"] = 0.5
    return d


def _valid_next() -> dict:
    return {
        "direction": "neutral",
        "confidence": "low",
        "move_target": None,
        "flip_trigger": "displacement through the day extreme",
        "flipped_target": None,
        "arm_entry_confirmation": "no",
        "resolution": {
            "long_if": {"condition": "reclaim + hold above mid", "price": None,
                        "target": None},
            "short_if": {"condition": "reject + hold below mid", "price": None,
                         "target": None},
        },
        "bull_ledger": [],
        "bear_ledger": [],
        "N": 0.0,
        "vetoes": [],
        "reasoning": "no accepted engine fire — stand aside",
    }


class FakeBackend(Backend):
    """Returns a preset queue of parsed decision dicts; records the messages sent."""

    name = "fake"

    def __init__(self, responses):
        self._queue = [copy.deepcopy(r) for r in responses]
        self.model = "fake-model"
        self.calls = []  # messages list per complete() invocation

    def complete(self, *, system, messages, schema, max_tokens=None):
        self.calls.append(copy.deepcopy(messages))
        parsed = self._queue.pop(0)
        return CallResponse(
            parsed=parsed,
            raw_text=json.dumps(parsed),
            usage={
                "input_tokens": 12000,
                "output_tokens": 400,
                "cache_read_input_tokens": 11000,
                "cache_creation_input_tokens": 0,
            },
            model=self.model,
        )


def _make_cut(tmp_path):
    dst = tmp_path / "cut"
    dst.mkdir()
    shutil.copy(os.path.join(SRC_CUT, "facts.txt"), dst / "facts.txt")
    shutil.copy(os.path.join(SRC_CUT, "context-at-cut.md"), dst / "context-at-cut.md")
    return str(dst)


# --------------------------------------------------------------------------- #
# 1. Happy path                                                                #
# --------------------------------------------------------------------------- #
def test_happy_path_writes_clean_decision(tmp_path):
    cut = _make_cut(tmp_path)
    backend = FakeBackend([_valid_daily(), _valid_next()])

    decision = run_cut(cut, backend)

    out = os.path.join(cut, "decision.json")
    assert os.path.exists(out)
    with open(out, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk == decision

    assert decision["protocol_clean"] is True
    assert decision["daily_trend"]["direction"] == "neutral"
    assert decision["next_move"]["direction"] == "neutral"
    assert decision["calls"]["daily_trend"]["retries"] == 0
    assert decision["calls"]["next_move"]["retries"] == 0
    assert decision["calls"]["daily_trend"]["fallback"] is False
    # reasoning is captured for the audit but kept OUT of the validated block
    assert "reasoning" not in decision["daily_trend"]
    assert decision["calls"]["daily_trend"]["reasoning"]
    # cache-read tokens are surfaced in the audit
    assert decision["calls"]["daily_trend"]["usage_total"]["cache_read_input_tokens"] > 0
    assert len(backend.calls) == 2


# --------------------------------------------------------------------------- #
# 2. Retry path — one invalid daily, then valid; violations quoted in reprompt #
# --------------------------------------------------------------------------- #
def test_retry_quotes_violations_then_succeeds(tmp_path):
    cut = _make_cut(tmp_path)
    backend = FakeBackend([_invalid_daily(), _valid_daily(), _valid_next()])

    decision = run_cut(cut, backend)

    daily = decision["calls"]["daily_trend"]
    assert daily["retries"] == 1
    assert daily["fallback"] is False
    assert decision["protocol_clean"] is True
    assert len(daily["attempts"]) == 2
    assert daily["attempts"][0]["ok"] is False
    assert any("ARI_FRACTIONAL_VOTE" in m for m in daily["attempts"][0]["violations"])

    # The second daily call (index 1) must carry the exact violation in a reprompt.
    reprompt = backend.calls[1]
    assert any(
        msg["role"] == "user" and "ARI_FRACTIONAL_VOTE" in msg["content"]
        for msg in reprompt
    )


# --------------------------------------------------------------------------- #
# 3. Failsafe path — invalid 3× → NEUTRAL/LOW fallback, flag set               #
# --------------------------------------------------------------------------- #
def test_failsafe_after_exhausting_retries(tmp_path):
    cut = _make_cut(tmp_path)
    backend = FakeBackend(
        [_invalid_daily(), _invalid_daily(), _invalid_daily(), _valid_next()]
    )

    decision = run_cut(cut, backend)

    daily = decision["calls"]["daily_trend"]
    assert daily["fallback"] is True
    assert daily["verdict"] == "failsafe"
    assert daily["retries"] == 2
    assert len(daily["attempts"]) == 3
    assert decision["protocol_clean"] is False
    # The emitted block is the canonical NEUTRAL/LOW fail-safe.
    assert decision["daily_trend"] == run_agent.failsafe_decision()["daily_trend"]


# --------------------------------------------------------------------------- #
# 4. Byte-stability — same inputs → byte-identical system prompt               #
# --------------------------------------------------------------------------- #
def test_system_prompt_byte_stable():
    a = build_system_prompt()
    b = build_system_prompt()
    assert a == b
    assert a.encode("utf-8") == b.encode("utf-8")
    # Fixed KB order, not glob order: smt first, next-move last.
    assert a.index("# FILE: smt.md") < a.index("# FILE: decisions/daily-trend.md")
    assert a.index("# FILE: decisions/daily-trend.md") < a.index("# FILE: decisions/next-move.md")


# --------------------------------------------------------------------------- #
# 5. .env loading — shell-set var wins over the .env value                     #
# --------------------------------------------------------------------------- #
def test_env_shell_var_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SHELL_WINS_KEY=from_env_file\nONLY_IN_FILE_KEY=from_env_file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHELL_WINS_KEY", "from_shell")
    monkeypatch.delenv("ONLY_IN_FILE_KEY", raising=False)

    load_env_file(str(env_file))

    assert os.environ["SHELL_WINS_KEY"] == "from_shell"      # shell precedence
    assert os.environ["ONLY_IN_FILE_KEY"] == "from_env_file"  # unset → loaded


# --------------------------------------------------------------------------- #
# 6. Backend auto-selection by key availability (--backend overrides)          #
# --------------------------------------------------------------------------- #
def test_backend_autoselect_prefers_openrouter(tmp_path, monkeypatch):
    empty_env = tmp_path / ".env"      # isolate from the real worktree .env
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")

    backend = make_backend(env_path=str(empty_env))
    assert isinstance(backend, OpenRouterBackend)


def test_backend_autoselect_falls_back_to_anthropic(tmp_path, monkeypatch):
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")

    backend = make_backend(env_path=str(empty_env))
    assert isinstance(backend, AnthropicBackend)


def test_backend_autoselect_errors_without_keys(tmp_path, monkeypatch):
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="No API key found"):
        make_backend(env_path=str(empty_env))


def test_backend_flag_overrides_autoselect(tmp_path, monkeypatch):
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")   # would auto-pick openrouter
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")

    backend = make_backend(backend="anthropic", env_path=str(empty_env))
    assert isinstance(backend, AnthropicBackend)
