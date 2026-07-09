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
    StubBackend,
    build_system_prompt,
    decide,
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


# --------------------------------------------------------------------------- #
# Wave 1.2 — decide() call-core extraction + offline stub backend.            #
# --------------------------------------------------------------------------- #
def _strip_latency(decision: dict) -> dict:
    """Zero every timing field so two runs of the SAME deterministic decision compare
    equal (latency is wall-clock and never reproducible)."""
    d = copy.deepcopy(decision)
    for call in d.get("calls", {}).values():
        call["latency_total_sec"] = 0.0
        for att in call.get("attempts", []):
            att["latency_sec"] = 0.0
    return d


def test_decide_equals_run_cut_on_same_cut(tmp_path):
    """decide() on a cut's in-memory facts == run_cut() on the same cut (minus the
    `cut` label and wall-clock latency), stub backend — the extraction is behaviour-
    preserving."""
    cut = _make_cut(tmp_path)
    with open(os.path.join(cut, "facts.txt"), encoding="utf-8") as fh:
        facts_text = fh.read()
    with open(os.path.join(cut, "context-at-cut.md"), encoding="utf-8") as fh:
        context_text = fh.read()
    facts = run_agent.parse_facts(facts_text)

    core = decide(facts_text, context_text, facts, StubBackend())
    full = run_cut(cut, StubBackend())

    assert full["cut"] == "cut"
    full_no_cut = {k: v for k, v in full.items() if k != "cut"}
    assert _strip_latency(full_no_cut) == _strip_latency(core)
    assert core["protocol_clean"] is True
    assert core["daily_trend"]["direction"] == "neutral"
    assert core["next_move"]["direction"] == "neutral"


def test_stub_backend_failsafe_on_repeated_invalid(tmp_path):
    cut = _make_cut(tmp_path)
    with open(os.path.join(cut, "facts.txt"), encoding="utf-8") as fh:
        facts_text = fh.read()
    facts = run_agent.parse_facts(facts_text)
    # daily fails validation on all 3 attempts (fractional vote) → failsafe; next clean.
    backend = StubBackend(responses=[_invalid_daily(), _invalid_daily(),
                                     _invalid_daily(), _valid_next()])
    core = decide(facts_text, "", facts, backend)

    assert core["calls"]["daily_trend"]["verdict"] == "failsafe"
    assert core["calls"]["daily_trend"]["fallback"] is True
    assert core["calls"]["daily_trend"]["retries"] == 2
    assert core["protocol_clean"] is False
    assert core["daily_trend"] == run_agent.failsafe_decision()["daily_trend"]


def test_stub_backend_retry_then_clean(tmp_path):
    cut = _make_cut(tmp_path)
    with open(os.path.join(cut, "facts.txt"), encoding="utf-8") as fh:
        facts_text = fh.read()
    facts = run_agent.parse_facts(facts_text)
    backend = StubBackend(responses=[_invalid_daily(), _valid_daily(), _valid_next()])
    core = decide(facts_text, "", facts, backend)

    assert core["calls"]["daily_trend"]["retries"] == 1
    assert core["calls"]["daily_trend"]["verdict"] == "clean"
    assert core["calls"]["daily_trend"]["fallback"] is False
    assert core["protocol_clean"] is True


def test_make_backend_stub_needs_no_keys(tmp_path, monkeypatch):
    """make_backend('stub') returns an offline backend without any API key."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backend = make_backend(backend="stub")
    assert isinstance(backend, StubBackend)
    # And a real backend with no keys still errors (the Wave-1.2 error path).
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="No API key found"):
        make_backend(env_path=str(empty_env))


# --------------------------------------------------------------------------- #
# 8. Code-derived arithmetic — model judges, code computes                     #
# --------------------------------------------------------------------------- #
def _ledger_item(tier=2.0, side=1.0, align=1.0, fresh=0.5, whip=1.0, score=None):
    return {"type": "sweep", "tier": tier, "session_side": side, "alignment": align,
            "freshness": fresh, "whipsaw": whip,
            "score": score if score is not None else tier * side * align * fresh * whip,
            "note": "t"}


def test_derive_next_fixes_scores_and_n():
    """The 2026-06-25 08:30 failure shape: correct bearish ledger, wrong declared
    N/score — code overrides both and records the disagreement."""
    block = {
        "direction": "down", "confidence": "medium",
        "bull_ledger": [],
        "bear_ledger": [_ledger_item(tier=3.0, fresh=1.0, score=0.95),   # wrong score
                        _ledger_item(tier=2.0, fresh=1.0)],
        "N": 5.4,                                                        # wrong sign
        "vetoes": [],
    }
    out, notes = run_agent._derive_next_arithmetic(block)
    assert out["bear_ledger"][0]["score"] == 3.0
    assert out["N"] == -5.0
    assert any("N 5.4" in n for n in notes)
    assert any(".score" in n for n in notes)
    # |N| = 5 → ceiling medium; declared medium stands.
    assert out["confidence"] == "medium"


def test_derive_next_confidence_ceiling_and_caps():
    # |N| = 2 → ceiling low even though the model claimed high.
    weak = {"direction": "neutral", "confidence": "high",
            "bull_ledger": [_ledger_item(tier=2.0, fresh=1.0)], "bear_ledger": [],
            "N": 2.0, "vetoes": []}
    out, notes = run_agent._derive_next_arithmetic(weak)
    assert out["confidence"] == "low" and any("confidence high -> low" in n for n in notes)

    # |N| ≥ 6 with empty losing ledger → high allowed... unless a cap_to_medium veto.
    strong = {"direction": "down", "confidence": "high",
              "bull_ledger": [],
              "bear_ledger": [_ledger_item(tier=3.0, side=1.5, fresh=1.0),
                              _ledger_item(tier=2.0, fresh=1.0)],
              "N": -6.5, "vetoes": [{"name": "v3", "triggered": True,
                                     "effect": "cap_to_medium"}]}
    out, _ = run_agent._derive_next_arithmetic(strong)
    assert out["confidence"] == "medium"

    # cap_to_low beats everything.
    strong["vetoes"] = [{"name": "v1", "triggered": True, "effect": "cap_to_low"}]
    strong["confidence"] = "high"
    out, _ = run_agent._derive_next_arithmetic(strong)
    assert out["confidence"] == "low"


def test_derive_next_never_touches_direction():
    block = {"direction": "up", "confidence": "low",
             "bull_ledger": [], "bear_ledger": [_ledger_item(tier=3.0, fresh=1.0)],
             "N": 3.0, "vetoes": []}
    out, _ = run_agent._derive_next_arithmetic(block)
    assert out["direction"] == "up"          # left for the validator/retry (judgment)
    assert out["N"] == -3.0                  # but the arithmetic is corrected


def test_derive_daily_fixes_s_and_clamps_confidence():
    d = _valid_daily()
    d["drivers"][0].update(vote=-1, contribution=-3.0)   # D1 w3
    d["drivers"][1].update(vote=-1, contribution=-2.0)   # D2 w2
    d["drivers"][3].update(vote=-1, contribution=-2.0)   # D4 w2
    d["S"] = -4.0                                        # wrong: sums to -7
    d["confidence"] = "high"                             # allowed: |S|>=6, no opposer
    out, notes = run_agent._derive_daily_arithmetic(d)
    assert out["S"] == -7.0 and any("S -4.0" in n for n in notes)
    assert out["confidence"] == "high"

    # A weight-≥2 opposer denies high even at |S| ≥ 6.
    d2 = _valid_daily()
    d2["drivers"][0].update(vote=-1, contribution=-3.0)
    d2["drivers"][1].update(vote=-1, contribution=-2.0)
    d2["drivers"][3].update(vote=-1, contribution=-2.0)
    d2["drivers"][2].update(vote=1, contribution=1.0)    # D3 w1 opposer — too light
    d2["S"] = -6.0
    d2["confidence"] = "high"
    out2, _ = run_agent._derive_daily_arithmetic(d2)
    assert out2["confidence"] == "high"                  # w1 opposer doesn't deny
    d2["drivers"][4].update(vote=1, weight=2, contribution=2.0)  # w2 opposer
    out3, _ = run_agent._derive_daily_arithmetic(d2)
    assert out3["confidence"] == "medium"


def test_run_call_derive_prevents_retry_on_bad_n(tmp_path):
    """End-to-end: a response with wrong N/scores but a consistent direction is now a
    clean ONE-SHOT (pre-derive it burned all retries and failsafed)."""
    cut = _make_cut(tmp_path)
    bad_n = _valid_next()
    bad_n["bear_ledger"] = [_ledger_item(tier=2.0, fresh=1.0)]
    bad_n["bull_ledger"] = []
    bad_n["N"] = 7.7                                     # nonsense; computes to -2.0
    bad_n["direction"] = "neutral"                       # consistent with computed N
    bad_n["confidence"] = "low"
    backend = StubBackend(responses=[_valid_daily(), bad_n])
    decision = run_cut(cut, backend)
    call = decision["calls"]["next_move"]
    assert call["retries"] == 0 and call["verdict"] == "clean"
    assert decision["next_move"]["N"] == -2.0
    assert any("N 7.7" in n for n in call["attempts"][0]["arith_overrides"])
