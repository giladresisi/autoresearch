import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from build_label_corpus import build  # noqa: E402

DATES = ["2026-08-11", "2026-08-13", "2026-08-14"]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("label-corpus")
    stats = build(str(out), dates=DATES)
    labels = [json.loads(l) for l in open(out / "labels.jsonl", encoding="utf-8")]
    cands = [json.loads(l) for l in open(out / "candidates.jsonl", encoding="utf-8")]
    return str(out), stats, labels, cands


def test_the_builder_labels_primary_and_secondary_segments_only(built):
    """Counter segments are observations (spec 4.3), not labelled units."""
    _out, _s, labels, _c = built
    assert {r["role"] for r in labels} <= {"primary", "secondary"}
    assert "primary" in {r["role"] for r in labels}


def test_every_requested_session_appears(built):
    _out, _s, labels, _c = built
    assert {r["date"] for r in labels} == set(DATES)


def test_each_segment_yields_one_row_per_instrument(built):
    """One label per instrument, never one per session -- spec 2.4 role-swap
    invariance."""
    _out, _s, labels, _c = built
    seen = {}
    for r in labels:
        seen.setdefault((r["date"], r["segment"]), set()).add(r["ticker"])
    assert all(v == {"MNQ", "MES"} for v in seen.values())


def test_an_unexplained_instrument_is_still_a_row(built):
    """An unexplained result must be VISIBLE in the corpus, never absent from it --
    the same reason the skeleton emits a bare row for a no-move session."""
    _out, _s, labels, _c = built
    assert all(r["status"] in ("labelled", "unexplained") for r in labels)
    assert all("names" in r and "price" in r for r in labels)


def test_the_candidate_rows_outnumber_the_labels(built):
    """One row per candidate per segment per instrument, not one per segment."""
    _out, _s, labels, cands = built
    assert len(cands) > 10 * len(labels)
    assert {r["outcome"] for r in cands} >= {"draw", "ineligible"}


def test_losers_are_recorded_not_only_winners(built):
    """Spec 3.4: a pool the move sailed through is evidence about its class's pull."""
    _out, _s, _l, cands = built
    assert any(r["outcome"] == "reached_passed" for r in cands)
    assert all(r["cls"] and r["tier"] for r in cands)


def test_the_holdout_flag_survives_into_the_corpus(built):
    _out, _s, labels, _c = built
    from agent.study.holdout import is_holdout
    import datetime
    for r in labels:
        assert r["holdout"] == is_holdout(datetime.date.fromisoformat(r["date"]))


def test_the_censored_flag_survives_into_the_corpus(built):
    """Spec 4.4: the long-monotone sessions are reported separately in every tally,
    so the flag has to reach the report."""
    _out, _s, labels, _c = built
    assert all(isinstance(r["censored"], bool) for r in labels)


def test_the_corpus_is_reproducible(tmp_path):
    """No wall clock, no unseeded randomness: two builds are byte-identical."""
    a, b = tmp_path / "a", tmp_path / "b"
    build(str(a), dates=DATES)
    build(str(b), dates=DATES)
    for name in ("labels.jsonl", "candidates.jsonl", "summary.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_the_summary_counts_agree_with_the_rows(built):
    _out, stats, labels, cands = built
    assert stats["label_rows"] == len(labels)
    assert stats["candidate_rows"] == len(cands)
    assert stats["labelled"] + stats["unexplained"] == len(labels)
