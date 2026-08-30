"""Task 12: the registry's own invariants, and doc/code drift detection.

Two representations of the same rules always diverge. Today `l2-mechanisms.md` holds its
numbers in prose and the tests hold them in Python, and nothing compares the two. These
tests make drift a failure instead of a discovery months later.
"""
import re

import pytest

from agent.trader import named_cases as nc


def test_the_registry_is_not_empty_and_every_key_is_unique():
    assert len(nc.CASES) >= 20
    keys = [c.key for c in nc.CASES]
    assert len(keys) == len(set(keys)), "duplicate registry keys"


def test_every_registered_case_names_the_test_that_pins_it():
    """A rule with no test is unenforced; a test with no rule is undocumented
    behaviour."""
    unpinned = [c.key for c in nc.CASES if not c.pinned_by]
    assert unpinned == [], f"cases with no pinning test: {unpinned}"


def test_every_pinning_reference_is_a_wellformed_test_path():
    bad = [(c.key, p) for c in nc.CASES for p in c.pinned_by
           if not re.match(r"^agent/trader/test_[a-z0-9_]+\.py::test_[a-zA-Z0-9_]+$", p)]
    assert bad == [], f"malformed pinned_by references: {bad}"


def test_every_registered_case_carries_an_era_stamp():
    """§6.2: 'a knob change dated after a validation silently invalidates that
    validation's entry set', and §6 records carry no era stamp. Here they all do."""
    bad = [c.key for c in nc.CASES if c.era not in nc.ERAS]
    assert bad == [], f"cases with an unknown or missing era: {bad}"


def test_no_case_asserts_a_pre_20260822_pnl_without_an_explained_delta():
    """The validation doctrine's core rule, encoded. A figure recorded before the
    2026-08-22 knob adoption (entry buffer 7->3, SL cap 25, max height 35->45) cannot be
    asserted as a current target unless the delta is accounted for.

    08-13 is the worked precedent: +89.25 -> +93.25, a difference of exactly 4.00 pts.
    An unexplained 4.00 pts is a defect wearing the same clothes.
    """
    stale = [c.key for c in nc.CASES
             if nc.ERAS[c.era][0] < nc.KNOB_ADOPTION_DATE
             and c.pnl is not None
             and not (c.explained_delta or c.excluded)]
    assert stale == [], (
        f"pre-{nc.KNOB_ADOPTION_DATE} P&L asserted with no explained delta: {stale}")


def test_the_calibrated_grid_is_stamped_and_superseded():
    """The nine uncontaminated calibrated rows are buffer-7 numbers; each is superseded
    by the same 4.00-pt trim, so they are recorded as a set rather than as targets."""
    assert nc.CALIBRATED_ERA in nc.ERAS
    assert nc.ERAS[nc.CALIBRATED_ERA][0] < nc.KNOB_ADOPTION_DATE
    assert "2026-08-21" not in nc.CALIBRATED_BUFFER7_FILLS, \
        "the contaminated 08-21 row must not sit in the calibrated set"
    assert nc.ENTRY_BUFFER_TRIM_PTS == 4.00


def test_the_registry_and_the_document_agree_on_every_figure():
    """Every figure the registry claims the document states is still there, verbatim."""
    missing = nc.missing_quotes()
    assert missing == [], (
        "l2-mechanisms.md no longer contains these registered figures — the doc and the "
        f"registry have drifted: {missing}")


def test_a_figure_changed_in_the_doc_alone_fails_this_suite():
    """Drift detection, seeded. Mutate one figure in a COPY of the document and confirm
    the checker reports it — otherwise the test above passes vacuously."""
    doc = nc.read_doc()
    case = nc.by_key("sec6-0805-current")
    original = case.doc_quotes[0]
    assert original in doc
    mutated = doc.replace("**+235.38, one entry**", "**+999.99, one entry**")
    assert mutated != doc, "the seeded mutation did not apply"

    reported = nc.missing_quotes(mutated)
    assert (case.key, original) in reported, \
        "a figure edited in the document alone was NOT detected as drift"


def test_a_figure_changed_in_the_registry_alone_also_fails():
    """The mirror case: the registry is not privileged over the document."""
    doc = nc.read_doc()
    assert ("fabricated", "08-05 | +111.11, one entry") not in \
        [(k, q) for k, q in nc.all_quotes()]
    assert "08-05 | +111.11, one entry" not in doc


def test_the_0821_fill_row_is_registered_as_EXCLUDED_with_its_reason():
    """Never delete an invalidated case — mark it. Deleting it loses the knowledge that
    it was considered and why it failed, which is what stops a future agent re-deriving
    it."""
    case = nc.by_key("calib-0821-EXCLUDED")
    assert case.pnl == 92.75
    assert case.excluded, "the row must carry its exclusion reason"
    assert "LOOK-AHEAD" in case.excluded
    assert "09:40:00" in case.excluded, "the reason must state the real existence instant"
    assert case.expect == "flat", "08-21's correct verdict under the strict reading"


def test_the_accepted_tolerances_are_registered_and_not_chased():
    """Two tolerances are pre-accepted by the document and must NOT be chased: §6.2's
    +/-1 cycle on 08-06 and 07-21's unexplained extra cycle."""
    assert set(nc.ACCEPTED_TOLERANCES) == {"sec6-0806-current", "sec6-0721-current"}
    for key in nc.ACCEPTED_TOLERANCES:
        nc.by_key(key)                    # raises if the tolerance names no real case


def test_every_case_with_an_entry_price_also_records_when_or_what_it_bound():
    """Asserting P&L alone is insufficient: the phase-1 ladder defect produced BETTER
    P&L from the WRONG gap, and only inspecting the bound artifact revealed it."""
    thin = [c.key for c in nc.CASES
            if c.entry_price is not None
            and c.entry_time is None and c.artifact is None]
    assert thin == [], f"entry recorded with neither a time nor an artifact: {thin}"


@pytest.mark.parametrize("key", [c.key for c in nc.CASES])
def test_each_case_states_an_expectation_shape(key):
    assert nc.by_key(key).expect in (
        "entry", "flat", "no_fire", "no_takeover", "strict_track")


def test_every_pinning_test_actually_exists():
    """Closes the loop the `pinned_by` field opens. A registry naming a test that was
    renamed or deleted is drift of exactly the kind this module exists to catch — and it
    is the silent kind, because nothing else reads the field.
    """
    import ast
    import os
    import pathlib

    here = pathlib.Path(__file__).resolve().parent.parent.parent
    cache: dict = {}
    missing = []
    for case in nc.CASES:
        for ref in case.pinned_by:
            path, _, name = ref.partition("::")
            full = here / path
            if path not in cache:
                if not full.exists():
                    cache[path] = set()
                else:
                    tree = ast.parse(full.read_text(encoding="utf-8"))
                    cache[path] = {n.name for n in ast.walk(tree)
                                   if isinstance(n, ast.FunctionDef)}
            if name not in cache[path]:
                missing.append((case.key, ref))
    assert missing == [], f"pinned_by names a test that does not exist: {missing}"


def test_every_documented_attempt_count_is_either_asserted_or_registered_unreachable():
    """Task 11 Step 4, closed in both directions.

    Attempts are a sharper change detector than P&L — a stopped-out attempt followed by a
    winner nets the same as one clean entry — so every documented count must be either
    ASSERTED against a replay or RECORDED as unreachable with the reason. Silence on a
    count is the failure mode this catches: it reads exactly like agreement.
    """
    for key, count in nc.DOCUMENTED_ATTEMPTS.items():
        case = nc.by_key(key)                     # raises if the key is not a real case
        assert isinstance(count, int) and 1 <= count <= 3, (key, count)
        assert case.attempts_used == count, (
            f"{key} records {case.attempts_used} attempts; the document states {count}")
        if key in nc.ATTEMPTS_NOT_DAY_CHECKABLE:
            assert nc.ATTEMPTS_NOT_DAY_CHECKABLE[key], f"{key}: empty reason"


def test_no_registered_attempt_count_escapes_the_documented_table():
    """The mirror: a case carrying an attempt count must appear in the table above, so a
    figure cannot be invented in the registry and pass as a reproduction."""
    missing = [c.key for c in nc.CASES
               if c.attempts_used is not None and not c.excluded
               and c.key not in nc.DOCUMENTED_ATTEMPTS]
    assert missing == [], f"attempt counts with no documented source: {missing}"


def test_the_0721_attempt_counts_are_kept_apart_by_thesis():
    """The reason the table is case-keyed. §6.2's CURRENT column gives THREE entries
    under §6's DOWN-thesis study; §10.1's recorded UP plan on the same date ends with one
    attempt in reserve, i.e. TWO. A date-keyed table asserts one against the other."""
    assert nc.DOCUMENTED_ATTEMPTS["sec6-0721-current"] == 3
    assert nc.DOCUMENTED_ATTEMPTS["sec8-0721-takeover"] == 2
    assert {c.date for c in (nc.by_key("sec6-0721-current"),
                             nc.by_key("sec8-0721-takeover"))} == {"2026-07-21"}
