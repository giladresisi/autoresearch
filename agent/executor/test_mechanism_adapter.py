"""Phase-4 mechanism adapter tests: each §7 enum maps to a code path (plan §Phase 4)."""

import pytest

from mechanism_adapter import (
    ENTRY_CODE_PATHS, LiveMechanismAdapter, MGMT_CODE_PATHS,
    RecordingMechanismAdapter, code_path_for,
)
from schemas import ENTRY_MECHANISMS, MGMT_MECHANISMS, TradePlan


def test_every_entry_enum_maps_to_a_code_path():
    for kind in ENTRY_MECHANISMS:
        cp = code_path_for(kind)
        assert cp and ":" in cp, kind


def test_every_mgmt_enum_maps_to_a_code_path():
    for kind in MGMT_MECHANISMS:
        cp = code_path_for(kind)
        assert cp and ":" in cp, kind


def test_mapping_covers_exactly_the_closed_enums():
    assert set(ENTRY_CODE_PATHS) == ENTRY_MECHANISMS
    assert set(MGMT_CODE_PATHS) == MGMT_MECHANISMS


def test_unknown_kind_has_no_code_path():
    assert code_path_for("telepathy") is None


@pytest.mark.parametrize("kind,expected", list(ENTRY_CODE_PATHS.items()))
def test_entry_code_path_values(kind, expected):
    assert code_path_for(kind) == expected


@pytest.mark.parametrize("kind,expected", list(MGMT_CODE_PATHS.items()))
def test_mgmt_code_path_values(kind, expected):
    assert code_path_for(kind) == expected


def test_recording_adapter_records_arm_with_code_paths():
    plan = TradePlan.from_dict({
        "verdict": "SETUP",
        "entry": {"direction": "LONG",
                  "mechanisms": [{"kind": "confirmation_bar", "params": {}, "valid_while": []},
                                 {"kind": "fvg_retrace", "params": {}, "valid_while": []}]},
    })
    a = RecordingMechanismAdapter()
    a.arm(plan)
    assert [k for k, _ in a.armed] == ["confirmation_bar", "fvg_retrace"]
    assert all(cp is not None for _, cp in a.armed)


def test_live_adapter_inert_when_disconnected():
    # A disconnected live adapter records intent but never resolves/calls the code path,
    # so it is safe to construct in a live dispatcher without any broker side effects.
    plan = TradePlan.from_dict({
        "verdict": "SETUP",
        "entry": {"direction": "LONG",
                  "mechanisms": [{"kind": "level_break_stop_entry", "params": {}, "valid_while": []}]},
    })
    a = LiveMechanismAdapter(broker=None, connected=False)
    a.arm(plan)
    a.execute_dol_falsified("MARKET_CLOSE", {})
    assert len(a.calls) == 2                        # intent recorded, nothing executed
