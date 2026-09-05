"""The machine-readable registry behind `l2-targets.md`.

Same problem as `agent/trader/named_cases.py`, same solution. The document holds its numbers
in prose and the artifacts hold them in JSON, and without something comparing the two they
drift — silently, and usually in the direction that flatters the result. Here every figure the
document quotes is a record, every record names the artifact it came from, and a number edited
in the document alone stops matching its `doc_quotes` and fails the drift test.

Three things each record carries that prose cannot:

  **SOURCE.** Which committed artifact the figure was read from — `stage_b_hazard.json`,
  `stage_c_holdout.json`, or a plan section for the measurements that predate the reports.
  A figure with no source cannot be re-derived and does not belong in the document.

  **`split`.** `discovery` or `holdout`. Cycle 4's single most repeatable mistake was quoting
  a discovery figure as though it were a result; B8g's own headline fell 15 points between the
  two. The split is mandatory on every performance record, and the suite asserts that the
  document never quotes a discovery accuracy without its holdout counterpart nearby.

  **`doc_quotes`.** Verbatim substrings of `l2-targets.md`.

BOUNDARY, held deliberately: this registry carries WHAT was measured and on which split. It
carries no thresholds the Executor would read and no implementation detail — nothing here is
implemented, and a registry that starts describing code is a specification beginning to rot.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
DOC = os.path.join(_REPO, "l2-targets.md")

SPLITS = ("discovery", "holdout", "both", "none")
KINDS = ("result", "negative", "registry", "definition", "candidate")

#: Committed artifacts a figure may be sourced from.
SOURCES = (
    ".agents/rule-search/stage_b_hazard.json",
    ".agents/rule-search/stage_c_holdout.json",
    ".agents/rule-search/stage_a_baseline.txt",
    ".agents/label-corpus/summary.json",
    ".agents/plans/29.cycle4-rule-search.md",
    ".agents/plans/28.cycle4-candidate-labelling.md",
    ".agents/plans/27.cycle4-session-skeleton.md",
    ".agents/plans/30.cycle4-decision-instant.md",
    ".agents/rule-search/decision_instant.json",
)


@dataclass(frozen=True)
class StudyCase:
    """One documented figure, with everything needed to re-derive or to refute it."""

    key: str
    kind: str                       # one of KINDS
    doc_section: str
    split: str                      # one of SPLITS
    source: str                     # one of SOURCES
    #: The claim in one line, as it would be said aloud.
    claim: str
    #: Sample size. REQUIRED on every result and registry record — the cycle's rare
    #: configurations are honest only when their n travels with them.
    n: "int | None" = None
    value: "float | None" = None
    doc_quotes: "tuple[str, ...]" = field(default_factory=tuple)
    note: str = ""


CASES: "tuple[StudyCase, ...]" = (
    # ---- the headline results, holdout first -------------------------------- #
    StudyCase(
        key="holdout-act-mnq", kind="result", doc_section="3.1", split="holdout",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="composed rule names the draw on 6 of 12 acted MNQ holdout sessions",
        n=12, value=0.5,
        doc_quotes=("**50.0%** (6/12)", "12/21 (57%)")),
    StudyCase(
        key="holdout-act-mes", kind="result", doc_section="3.1", split="holdout",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="composed rule names the draw on 7 of 13 acted MES holdout sessions",
        n=13, value=0.5385,
        doc_quotes=("**53.9%** (7/13)", "13/18 (72%)")),
    StudyCase(
        key="holdout-unconditional-mnq", kind="result", doc_section="3.1", split="holdout",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="acting on every MNQ holdout session gives 38.1%", n=21, value=0.381,
        doc_quotes=("38.1%",)),
    StudyCase(
        key="holdout-unconditional-mes", kind="result", doc_section="3.1", split="holdout",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="acting on every MES holdout session gives 44.4%", n=18, value=0.4444,
        doc_quotes=("44.4%",)),
    StudyCase(
        key="b9-holdout-purity", kind="result", doc_section="6", split="holdout",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="every session the composed rule acted on had a pool-based answer",
        n=25, value=1.0,
        doc_quotes=("**100% on\nboth instruments**", "12 of 12 and 13 of 13"),
        note="12 MNQ + 13 MES. The cleanest single result of the cycle."),

    # ---- discovery, always paired with its holdout counterpart -------------- #
    StudyCase(
        key="discovery-b8g-mnq", kind="result", doc_section="3.2", split="discovery",
        source=".agents/rule-search/stage_b_hazard.json",
        claim="B8g named the draw on 30 of 48 labelled MNQ discovery segments",
        n=48, value=0.625,
        doc_quotes=("**30 of 48** labelled MNQ segments (62.5%, Brier 0.1746)",)),
    StudyCase(
        key="discovery-b8g-mes", kind="result", doc_section="3.2", split="discovery",
        source=".agents/rule-search/stage_b_hazard.json",
        claim="B8g named the draw on 30 of 44 labelled MES discovery segments",
        n=44, value=0.6818,
        doc_quotes=("**30 of 44**\n  MES (68.2%, Brier 0.1770)",)),
    StudyCase(
        key="discovery-vs-distance-only", kind="result", doc_section="3.2", split="discovery",
        source=".agents/rule-search/stage_b_hazard.json",
        claim="the gap beats a distance-only hazard on both instruments",
        n=92, value=0.2083,
        doc_quotes=("**+20.8 pp [+10.4, +31.2]** on MNQ and **+18.2 pp\n  [+9.1, +27.3]** on MES",)),
    StudyCase(
        key="optimism-gap", kind="result", doc_section="3.2", split="both",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="the selection rule lost about 15 points between discovery and holdout",
        n=25, value=0.15,
        doc_quotes=("**The selection rule was optimistic by roughly 15 points.**",
                    "low-to-mid 50s act\naccuracy at roughly 60% coverage")),
    StudyCase(
        key="permutation-gate", kind="result", doc_section="3.3", split="discovery",
        source=".agents/rule-search/stage_b_hazard.json",
        claim="the chaining effect survives a rank-stratified permutation on both instruments",
        n=193, value=0.0005,
        doc_quotes=("+1.34 vs +0.13", "+1.30 vs +0.10"),
        note="193 pool-observations with a gap ahead, 97 MNQ + 96 MES."),

    # ---- the negatives ------------------------------------------------------ #
    StudyCase(
        key="neg-tier", kind="negative", doc_section="4", split="discovery",
        source=".agents/rule-search/stage_b_hazard.json",
        claim="pool class carries nothing, marginally or in interaction with the gap",
        n=92, value=-0.0417,
        doc_quotes=("adding tier costs 4.2 pp on MNQ and adds nothing on MES",)),
    StudyCase(
        key="neg-mes-anchor", kind="negative", doc_section="4", split="discovery",
        source=".agents/rule-search/stage_b_hazard.json",
        claim="the counterpart's geometry makes the rule worse, not better",
        n=92, value=-0.125,
        doc_quotes=("costs **12.5 pp** on MNQ and 4.5 pp on MES",)),
    StudyCase(
        key="neg-counterpart-unexplained", kind="negative", doc_section="4", split="discovery",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="unexplained sessions are positively correlated across instruments",
        n=63, value=-0.23,
        doc_quotes=("**47%** given MNQ unexplained", "8 doubly-unexplained segments")),
    StudyCase(
        key="neg-fvg-coverage", kind="negative", doc_section="4", split="discovery",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="FVG edges sit only in large gaps but do not reach the unexplained halts",
        n=216, value=0.0,
        doc_quotes=("**0 of 216** small named gaps",
                    "a median 3.15 × avg_1h from the nearest edge")),

    # ---- the registry: too rare to rate ------------------------------------- #
    StudyCase(
        key="reg-day-then-week", kind="registry", doc_section="5.1", split="discovery",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="a week pool just beyond a reachable day pool was taken every time it appeared",
        n=4, value=1.0,
        doc_quotes=("**4 of 48** MNQ discovery segments", "**all four**"),
        note="n=4. Not a rate. Quoting it without its n is the failure this record exists "
             "to prevent."),
    StudyCase(
        key="reg-0813-overshoot", kind="registry", doc_section="5.2", split="discovery",
        source=".agents/plans/28.cycle4-candidate-labelling.md",
        claim="2026-08-13 overshot its draw by 193.75 points without a further target",
        n=1, value=193.75,
        doc_quotes=("**193.75 points** of overshoot", "29862.75 → 30267.00")),
    StudyCase(
        key="reg-0811-asymmetry", kind="registry", doc_section="5.3", split="discovery",
        source=".agents/plans/28.cycle4-candidate-labelling.md",
        claim="2026-08-11 MNQ swept its day low by 30 points while MES fell 5.0 short",
        n=1, value=5.0,
        doc_quotes=("**30 points** through it, while MES\nfell **5.0 points** short",
                    "0.51 × MES's `avg_range_1h`")),
    StudyCase(
        key="reg-divergence-veto", kind="registry", doc_section="5.4", split="none",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="the counterpart-divergence veto left the cycle unmeasured, not refuted",
        n=0,
        doc_quotes=("never built",),
        note="n=0 deliberately: measured zero times. Distinguishes 'never tested' from "
             "'tested and found nothing', which the deferral list must not blur."),

    # ---- definitions the document leans on ---------------------------------- #
    StudyCase(
        key="def-scope", kind="definition", doc_section="0", split="none",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="cycle 4 ends at the fill-time decision",
        doc_quotes=("the one-off target decision taken shortly after the entry fill,\nand "
                    "nothing beyond it",)),
    StudyCase(
        key="def-act-accuracy", kind="definition", doc_section="3.1", split="none",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="act accuracy counts an unexplained acted session as a miss",
        doc_quotes=("An unexplained session inside the acted\nset counts as a miss",)),
    # ---- phase 5: step 0 said not yet -------------------------------------- #
    StudyCase(
        key="cand-b9-step0", kind="candidate", doc_section="9", split="none",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="B9 failed step 0 of the change protocol and is recorded as a CANDIDATE",
        doc_quotes=("**Status: CANDIDATE. Not implemented, and must not be implemented from",
                    "**DO NOT** implement a hard decline from this text."),
        note="The protocol's own path: CANDIDATE -> resolve -> a rule -> the eight steps."),
    StudyCase(
        key="cand-b9-units", kind="candidate", doc_section="9.3", split="none",
        source=".agents/rule-search/stage_b_hazard.json",
        claim="the threshold was measured from the move origin, not from the fill",
        value=2.0,
        doc_quotes=("**210.5 points at the origin to 127.8 at",
                    "**This is the blocking one**"),
        note="A defect in how the study was framed, not in the finding."),
    StudyCase(
        key="cand-b9-projection-conflict", kind="candidate", doc_section="9.3", split="none",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="production already answers B9's condition by emitting a projection draw",
        doc_quotes=("where the system currently projects.",
                    "hard exclusion would silently rewrite no-liquidity")),
    StudyCase(
        key="cand-b9-decline-ambiguity", kind="candidate", doc_section="9.3", split="none",
        source=".agents/plans/29.cycle4-rule-search.md",
        claim="'decline to set a target' has three readings and the document does not choose",
        doc_quotes=("Three mechanisms, three P&L profiles.",)),
    # ---- plan 30: the anchor fix -------------------------------------------- #
    StudyCase(
        key="p30-b8g-ports", kind="result", doc_section="2.2", split="discovery",
        source=".agents/rule-search/decision_instant.json",
        claim="B8g survives re-derivation at a production-computable clock instant",
        n=48, value=0.636,
        doc_quotes=("**This rule ports (plan 30, 2026-09-05).**",
                    "**The rule was not an artefact of"),
        note="Its feature is pool-to-pool and never references the anchor, which is why it "
             "was the half expected to survive."),
    StudyCase(
        key="p30-anchor-coverage-cost", kind="result", doc_section="2.2", split="discovery",
        source=".agents/rule-search/decision_instant.json",
        claim="a clock anchor cannot name a target once the draw has been passed",
        n=92, value=0.21,
        doc_quotes=("**56–58% (MNQ)** and **46–48% (MES)**",),
        note="MNQ loses ~5 points, MES ~21. MES's draws sit nearer and are taken sooner."),
    StudyCase(
        key="p30-threshold-converts", kind="result", doc_section="9.4", split="discovery",
        source=".agents/plans/30.cycle4-decision-instant.md",
        claim="B9's threshold converts from 2.0 at the origin to about 1.0 from now_price",
        n=43, value=1.0,
        doc_quotes=("**1.0 × avg_range_1h**, not 2.0",
                    "The mechanism translates; only its number was"),
        note="Plan 30 predicted ~1.2 before measuring; the measured value is 1.0."),
    StudyCase(
        key="p30-discovery-grade", kind="definition", doc_section="9.4", split="discovery",
        source=".agents/plans/30.cycle4-decision-instant.md",
        claim="plan 30's figures carry two re-fitted parameters and no out-of-sample check",
        doc_quotes=("**Discovery-grade only**",)),
    StudyCase(
        key="forward-holdout-reserved", kind="definition", doc_section="10", split="none",
        source=".agents/plans/30.cycle4-decision-instant.md",
        claim="sessions after 2026-09-02 are reserved unread as the only clean validator",
        doc_quotes=("**Sessions after 2026-09-02 are reserved and unread. Do not look at them.**",
                    "the decision expires by being deferred")),

    StudyCase(
        key="def-holdout-spent", kind="definition", doc_section="8", split="holdout",
        source=".agents/rule-search/stage_c_holdout.json",
        claim="the holdout is spent and cannot be refilled from this data range",
        n=21,
        doc_quotes=("**The holdout is spent.**",)),
)


def by_key(key: str) -> StudyCase:
    for c in CASES:
        if c.key == key:
            return c
    raise KeyError(key)


def of_kind(kind: str) -> "list[StudyCase]":
    return [c for c in CASES if c.kind == kind]


def read_doc(path: str = DOC) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def all_quotes() -> "list[tuple[str, str]]":
    return [(c.key, q) for c in CASES for q in c.doc_quotes]


def missing_quotes(doc_text: "str | None" = None) -> "list[tuple[str, str]]":
    """Every (case key, quote) the document no longer contains — the drift detector.

    A figure edited in `l2-targets.md` alone stops matching here and the suite fails,
    instead of the divergence being found months later by someone quoting a number that
    the artifacts no longer support.
    """
    text = doc_text if doc_text is not None else read_doc()
    return [(k, q) for k, q in all_quotes() if q not in text]
