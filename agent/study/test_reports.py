"""Smoke tests for the two phase-3 report scripts.

They are not decorative: both reports are the only reproducible record of numbers quoted in
plan 29, and a report that raises on the committed corpus makes those numbers unverifiable.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
CORPUS = os.path.join(_REPO, ".agents", "label-corpus")


@pytest.fixture(scope="module")
def hazard_payload(tmp_path_factory):
    from report_hazard import report
    return report(CORPUS, str(tmp_path_factory.mktemp("rule-search")))


def test_the_baseline_report_runs_on_the_committed_corpus(capsys):
    from report_baseline import report
    report(CORPUS)
    out = capsys.readouterr().out
    assert "THE NUMBER TO BEAT" in out
    assert "holdout not read" in out


def test_the_hazard_report_runs_and_emits_every_family(hazard_payload):
    for fam in ("B0", "B8", "B8g", "B8xB4"):
        for tk in ("MNQ", "MES"):
            assert "%s/%s" % (fam, tk) in hazard_payload["families"]


def test_the_permutation_gate_is_recorded_for_both_instruments(hazard_payload):
    for tk in ("MNQ", "MES"):
        assert hazard_payload["permutation"][tk]["p"] < 0.05


def test_b8_beats_the_distance_only_hazard_on_both_instruments(hazard_payload):
    """The headline of plan 29 §B.5. If this fails, the plan is quoting stale numbers."""
    for tk in ("MNQ", "MES"):
        d = hazard_payload["deltas"]["B8-B0/%s" % tk]
        assert d["delta"] > 0.10
        assert d["lo"] > 0


def test_the_gap_only_rule_is_not_worse_than_the_two_feature_one(hazard_payload):
    """B8g is the shipped shape; it must not be bought at a cost in accuracy."""
    for tk in ("MNQ", "MES"):
        assert hazard_payload["deltas"]["B8g-B8/%s" % tk]["delta"] >= 0


def test_abstaining_beats_acting_on_everything(hazard_payload):
    """B9's whole claim: naming a target only where a pool is near raises the share of
    ACTED sessions we get right, even though it lowers coverage."""
    for tk in ("MNQ", "MES"):
        rows = {r["threshold"]: r for r in hazard_payload["abstain"][tk]}
        assert rows[2.0]["act_accuracy"] > rows[None]["act_accuracy"] + 0.10
        assert rows[2.0]["coverage"] < 1.0


def test_the_report_writes_a_diffable_artifact(tmp_path):
    from report_hazard import report
    report(CORPUS, str(tmp_path))
    saved = json.load(open(tmp_path / "stage_b_hazard.json", encoding="utf-8"))
    assert set(saved) == {"families", "permutation", "deltas", "tables", "abstain"}
