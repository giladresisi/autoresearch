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


# --------------------------------------------------------------------------- #
# Stage C -- the spent holdout. These tests re-run a measurement that is already
# spent; they guard the recorded result against drift, they do not spend anything.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def holdout_payload(tmp_path_factory):
    from report_holdout import report
    return report(CORPUS, str(tmp_path_factory.mktemp("stage-c")))


def test_the_holdout_script_trains_on_discovery_and_scores_the_held_back_sessions(
        holdout_payload):
    assert holdout_payload["family"] == "B8g"
    assert holdout_payload["abstain_max_d0"] == 2.0
    for tk in ("MNQ", "MES"):
        assert holdout_payload["per_ticker"][tk]["everything"]["n_total"] > 0


def test_the_abstain_rule_excluded_every_unexplainable_holdout_session(holdout_payload):
    """B9's claim, on data it never saw: of the sessions the composed rule ACTS on,
    all of them had a pool-based answer. 12/12 MNQ and 13/13 MES."""
    for tk in ("MNQ", "MES"):
        assert holdout_payload["per_ticker"][tk]["acted"]["p_labelled"] == 1.0


def test_abstaining_still_beat_acting_on_everything_out_of_sample(holdout_payload):
    for tk in ("MNQ", "MES"):
        r = holdout_payload["per_ticker"][tk]
        assert r["acted"]["act_accuracy"] > r["everything"]["act_accuracy"]
        assert r["acted"]["coverage"] < 1.0


def test_the_holdout_result_is_recorded_at_its_measured_value(holdout_payload):
    """Pins the numbers plan 29 §C quotes. The holdout cannot be re-spent, so a change
    here means the corpus or the rule moved underneath a result that is final."""
    mnq = holdout_payload["per_ticker"]["MNQ"]["acted"]
    mes = holdout_payload["per_ticker"]["MES"]["acted"]
    assert (mnq["n_covered"], mnq["act_accuracy"]) == (12, 0.5)
    assert (mes["n_covered"], mes["act_accuracy"]) == (13, pytest.approx(0.5385, abs=1e-3))
