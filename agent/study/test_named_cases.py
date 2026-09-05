"""Drift detection between `l2-targets.md` and the artifacts behind it.

Two representations of the same results always diverge. The document holds its numbers in
prose and `.agents/rule-search/` holds them in JSON; these tests make the divergence a
failure rather than a discovery months later by someone quoting a figure the data no longer
supports.
"""
import json
import os

import pytest

from agent.study import named_cases as nc

ARTIFACTS = os.path.join(nc._REPO, ".agents", "rule-search")


def _holdout():
    with open(os.path.join(ARTIFACTS, "stage_c_holdout.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _stage_b():
    with open(os.path.join(ARTIFACTS, "stage_b_hazard.json"), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# registry invariants
# --------------------------------------------------------------------------- #
def test_the_registry_is_populated_and_keys_are_unique():
    assert len(nc.CASES) >= 15
    keys = [c.key for c in nc.CASES]
    assert len(keys) == len(set(keys)), "duplicate registry keys"


def test_every_case_declares_a_known_kind_and_split():
    bad = [(c.key, c.kind, c.split) for c in nc.CASES
           if c.kind not in nc.KINDS or c.split not in nc.SPLITS]
    assert bad == [], f"unknown kind/split: {bad}"


def test_every_case_names_a_committed_artifact_as_its_source():
    """A figure that cannot be re-derived does not belong in the document."""
    bad = [(c.key, c.source) for c in nc.CASES if c.source not in nc.SOURCES]
    assert bad == [], f"unknown sources: {bad}"
    missing = [(c.key, c.source) for c in nc.CASES
               if not os.path.exists(os.path.join(nc._REPO, c.source))]
    assert missing == [], f"sources that do not exist: {missing}"


def test_every_result_and_registry_case_carries_its_sample_size():
    """The cycle's rare configurations are honest only when their n travels with them —
    5.1 is 4 of 4, and quoting it without the 4 is the failure this prevents."""
    bad = [c.key for c in nc.CASES
           if c.kind in ("result", "registry") and c.n is None]
    assert bad == [], f"result/registry cases with no n: {bad}"


def test_every_case_states_its_claim_in_a_sentence():
    bad = [c.key for c in nc.CASES if len(c.claim) < 20]
    assert bad == [], f"cases with no readable claim: {bad}"


# --------------------------------------------------------------------------- #
# the drift detector
# --------------------------------------------------------------------------- #
def test_the_document_still_contains_every_quoted_figure():
    missing = nc.missing_quotes()
    assert missing == [], (
        "l2-targets.md no longer contains these registry quotes — the document and the "
        "registry have drifted:\n" + "\n".join("  %s: %r" % m for m in missing))


def test_every_case_pins_at_least_one_quote_except_the_unmeasured_one():
    unquoted = [c.key for c in nc.CASES if not c.doc_quotes]
    assert unquoted == [], f"cases pinning nothing in the document: {unquoted}"


# --------------------------------------------------------------------------- #
# the figures themselves, against the artifacts
# --------------------------------------------------------------------------- #
def test_the_holdout_figures_match_the_committed_artifact():
    h = _holdout()["per_ticker"]
    assert h["MNQ"]["acted"]["n_covered"] == nc.by_key("holdout-act-mnq").n
    assert h["MNQ"]["acted"]["act_accuracy"] == pytest.approx(
        nc.by_key("holdout-act-mnq").value, abs=1e-3)
    assert h["MES"]["acted"]["act_accuracy"] == pytest.approx(
        nc.by_key("holdout-act-mes").value, abs=1e-3)
    assert h["MNQ"]["everything"]["act_accuracy"] == pytest.approx(
        nc.by_key("holdout-unconditional-mnq").value, abs=1e-3)


def test_the_discovery_figures_match_the_committed_artifact():
    fam = _stage_b()["families"]
    assert fam["B8g/MNQ"]["acc"] == pytest.approx(
        nc.by_key("discovery-b8g-mnq").value, abs=1e-3)
    assert fam["B8g/MES"]["acc"] == pytest.approx(
        nc.by_key("discovery-b8g-mes").value, abs=1e-3)


def test_the_abstain_rule_was_perfect_on_the_holdout():
    """The claim §6 rests its shipping recommendation on."""
    h = _holdout()["per_ticker"]
    for tk in ("MNQ", "MES"):
        assert h[tk]["acted"]["p_labelled"] == 1.0
    assert nc.by_key("b9-holdout-purity").value == 1.0


def test_the_recorded_optimism_gap_is_real():
    """B8g fell from discovery to holdout; the document must not quote the discovery
    figure as the result."""
    h = _holdout()["per_ticker"]
    b = _stage_b()["families"]
    for tk in ("MNQ", "MES"):
        assert h[tk]["acted"]["acc_given_labelled"] < b["B8g/%s" % tk]["acc"]


# --------------------------------------------------------------------------- #
# the discipline the document is supposed to carry
# --------------------------------------------------------------------------- #
def test_the_document_leads_with_the_holdout_not_the_discovery_figure():
    """Cycle 4's single most repeatable mistake was quoting a discovery figure as a
    result. The holdout section must precede the discovery section in the document."""
    doc = nc.read_doc()
    assert doc.index("### 3.1 The holdout") < doc.index("### 3.2 Discovery")


def test_the_document_warns_against_quoting_the_fitted_number():
    doc = nc.read_doc()
    assert "Any document quoting 62–68% is quoting a fitted number." in doc


def test_the_document_states_that_it_is_not_implemented():
    """Nothing here runs, and a reader must learn that in the first lines."""
    assert "**Status: study output, not implemented.**" in nc.read_doc()[:400]


def test_the_document_refuses_to_be_merged_into_l2_mechanisms():
    doc = nc.read_doc()
    assert "must never be merged into it" in doc
    assert os.path.exists(os.path.join(nc._REPO, "l2-mechanisms.md"))


def test_every_negative_result_is_recorded_rather_than_dropped():
    """The two survivors are believable only in the company of what failed."""
    negatives = nc.of_kind("negative")
    assert len(negatives) >= 4
    doc = nc.read_doc()
    assert "## 4. What was refuted" in doc


def test_the_deferred_work_is_named_rather_than_forgotten():
    doc = nc.read_doc()
    for item in ("Mid-move target revision", "counterpart-divergence veto",
                 "tiered give-back exit ladder"):
        assert item in doc, item


# --------------------------------------------------------------------------- #
# phase 5: the step-0 verdict must stay visible
# --------------------------------------------------------------------------- #
def test_the_candidate_tier_is_recorded_and_marked_unimplemented():
    """B9 failed step 0. The document must say so where a reader of the RULE will see
    it, not only in a section they might skip."""
    cands = nc.of_kind("candidate")
    assert len(cands) >= 4
    doc = nc.read_doc()
    assert "## 9. CANDIDATE" in doc
    assert "**DO NOT** implement a hard decline from this text." in doc


def test_the_rule_section_itself_warns_that_it_is_not_a_rule():
    """§2.1 reads like a rule. A reader who stops there must still learn that step 0
    rejected it — the warning lives inside §2, not only in §9."""
    doc = nc.read_doc()
    rules = doc[doc.index("## 2. The rules"):doc.index("## 3. Evidence")]
    assert "NOT A RULE YET" in rules
    assert "§9" in rules


def test_the_status_header_points_at_the_step0_verdict():
    assert "did\nNOT pass step 0" in nc.read_doc()[:500]


# --------------------------------------------------------------------------- #
# plan 30: the anchor fix, and the reservation it forced
# --------------------------------------------------------------------------- #
def test_the_document_records_which_rule_ported_and_which_did_not():
    """Plan 30's whole point: B8g's feature is anchor-free and B9's was not. A document
    that reports only the survivor hides the asymmetry that made it worth testing."""
    doc = nc.read_doc()
    assert "**This rule ports (plan 30, 2026-09-05).**" in doc
    assert "1.0 × avg_range_1h**, not 2.0" in doc


def test_the_coverage_cost_of_a_computable_anchor_is_stated():
    """Accuracy on USABLE sessions flatters a clock anchor, because sessions whose draw
    has already been passed simply vanish. The all-session figure must be present."""
    assert "**56–58% (MNQ)** and **46–48% (MES)**" in nc.read_doc()


def test_plan_30_figures_are_marked_discovery_grade():
    """Two re-fitted parameters, 36 cells, no holdout left. Quoting them as results is
    the same mistake cycle 4 made and corrected."""
    assert "**Discovery-grade only**" in nc.read_doc()


def test_the_forward_holdout_reservation_is_recorded():
    """It is clean only for as long as nobody looks, so the commitment has to exist
    before the work that would be validated against it."""
    doc = nc.read_doc()
    assert "**Sessions after 2026-09-02 are reserved and unread. Do not look at them.**" in doc
    assert "## 10. The forward holdout is RESERVED" in doc


def test_the_reservation_records_why_historical_blocks_were_rejected():
    """2026-04 was a sustained uptrend; a single historical block tests regime transfer
    rather than the rule."""
    doc = nc.read_doc()
    assert "never pooled" in doc
    assert "unusual sustained" in doc
