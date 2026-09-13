"""Phase-4 ACT_AI_MODE flag resolution tests (plan §Phase 4 / spec §3)."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import decisions_config as sc  # noqa: E402


def _resolve(monkeypatch, mode=None, legacy=None):
    monkeypatch.delenv("ACT_AI_MODE", raising=False)
    monkeypatch.delenv("ACT_AI_DECISIONS", raising=False)
    if mode is not None:
        monkeypatch.setenv("ACT_AI_MODE", mode)
    if legacy is not None:
        monkeypatch.setenv("ACT_AI_DECISIONS", legacy)
    return sc.resolve_ai_mode()


def test_default_is_off(monkeypatch):
    assert _resolve(monkeypatch) == "off"


def test_explicit_modes(monkeypatch):
    assert _resolve(monkeypatch, mode="off") == "off"
    assert _resolve(monkeypatch, mode="shadow") == "shadow"
    assert _resolve(monkeypatch, mode="primary") == "primary"
    assert _resolve(monkeypatch, mode="PRIMARY") == "primary"        # case-insensitive


def test_unknown_mode_falls_back_to_off(monkeypatch):
    assert _resolve(monkeypatch, mode="banana") == "off"


def test_legacy_alias_maps_to_shadow(monkeypatch):
    assert _resolve(monkeypatch, legacy="1") == "shadow"
    assert _resolve(monkeypatch, legacy="true") == "shadow"


def test_act_ai_mode_takes_precedence_over_legacy(monkeypatch):
    assert _resolve(monkeypatch, mode="primary", legacy="1") == "primary"
    assert _resolve(monkeypatch, mode="off", legacy="1") == "off"


def test_module_constants_consistent_with_off_default():
    # As imported (no env set in CI), the module resolves to off ⇒ both gates False ⇒
    # byte-identical regressions.
    if sc.AI_MODE == "off":
        assert sc.AI_DECISIONS_ENABLED is False
        assert sc.AI_PRIMARY_ENABLED is False
