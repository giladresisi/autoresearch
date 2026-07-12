"""Phase-5 confidence module tests (plan §Phase 5 / spec §8)."""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import confidence as C  # noqa: E402


def _decision():
    """A v1 next_move-shaped decision with a known ledger (the calibration substrate)."""
    return {
        "daily_trend": {"regime": "trend"},
        "next_move": {
            "N": -4.78, "confidence": "medium",
            "bull_ledger": [
                {"type": "sustained_acceptance", "tier": 2.0, "freshness": 0.98, "score": 1.18},
            ],
            "bear_ledger": [
                {"type": "sweep", "tier": 3.0, "freshness": 0.90, "score": 2.7},
                {"type": "smt_divergence", "tier": 1.5, "freshness": 0.80, "score": 1.2},
            ],
        },
    }


def test_feature_extraction_matches_hand_computed():
    f = C.extract_features(_decision())
    assert f["evidence_count"] == 3
    assert f["item_type_diversity"] == 3               # 3 distinct types
    assert f["tier_max"] == 3.0
    assert f["freshness_min"] == 0.80
    assert abs(f["freshness_mean"] - round((0.98 + 0.90 + 0.80) / 3, 4)) < 1e-9
    assert abs(f["score_abs"] - 4.78) < 1e-9
    assert f["regime"] == "TREND"


def test_cell_key_bands():
    assert C.cell_key(C.extract_features(_decision())) == "TREND|3-6|3-5"


def test_missing_cell_returns_floor():
    tbl = C.CalibrationTable(cells={}, floor="LOW")
    assert C.confidence(_decision(), table=tbl) == "LOW"


def test_under_sampled_cell_returns_floor():
    key = C.cell_key(C.extract_features(_decision()))
    tbl = C.CalibrationTable(cells={key: {"hit_rate": 0.99, "n": 2}}, min_samples=5)
    assert C.confidence(_decision(), table=tbl) == "LOW"   # n below min_samples → floor


def test_gate_thresholds_respected():
    tbl = C.CalibrationTable(thresholds={"high": 0.6, "medium": 0.45})
    assert tbl.gate(0.70) == "HIGH"
    assert tbl.gate(0.50) == "MEDIUM"
    assert tbl.gate(0.30) == "LOW"
    assert tbl.gate(None) == "LOW"


def test_confidence_uses_calibration_not_self_report():
    # self-report says 'medium', but the calibrated cell (well-sampled, high hit-rate) is HIGH.
    key = C.cell_key(C.extract_features(_decision()))
    tbl = C.CalibrationTable(cells={key: {"hit_rate": 0.8, "n": 20}})
    assert C.confidence(_decision(), table=tbl) == "HIGH"


def test_ledgerless_thesis_falls_to_floor():
    thesis = {"bias": "UP", "regime": "TREND", "confidence": "HIGH"}
    assert C.confidence(thesis, table=C.CalibrationTable(cells={})) == "LOW"


def test_bootstrap_build_from_batch_audits(tmp_path):
    import glob
    paths = sorted(glob.glob(os.path.join(os.path.dirname(_HERE), "regression", "sessions",
                                          "*", "*", "ai_decisions_audit.jsonl")))
    if not paths:
        import pytest
        pytest.skip("no batch audit files present")
    tbl = C.build_bootstrap_table(paths)
    assert tbl.cells                                   # non-empty
    for cell in tbl.cells.values():
        assert 0.0 <= cell["hit_rate"] <= 1.0 and cell["n"] > 0
    out = tmp_path / "table.json"
    tbl.save(out)
    assert C.CalibrationTable.load(out).cells == tbl.cells


def test_default_table_artifact_present_and_loadable():
    # The committed bootstrap artifact loads and gates deterministically.
    tbl = C.CalibrationTable.load(C.DEFAULT_TABLE_PATH)
    assert isinstance(tbl.cells, dict)
    assert C.confidence(_decision(), facts=None) in C.TIERS
