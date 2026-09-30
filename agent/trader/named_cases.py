"""The machine-readable registry of every documented regression case.

`l2-mechanisms.md` is the specification, the evidence ledger, and the source of every
regression figure the entry mechanisms are measured against. Today the doc holds its
numbers in prose and the tests hold them in Python, and nothing compares the two — a
gap this cycle produced THREE instances of in one session (the doc pinned FVG existence
while the code gated on identity; the doc re-keyed §7 from distance to age while the code
had neither; the §7 SL-cap comparison inverted mid-session while it was being quoted).
Divergence between two representations of the same rules is inevitable; being SURPRISED
by it is not. This module makes drift a test failure.

Three things each record carries that prose cannot:

  **ERA.** Recorded figures were produced under knob sets that no longer exist. §6.2
  discovered the general form of the problem — "a knob change dated AFTER a validation
  silently invalidates that validation's entry set" — and §6 records carry no era stamp.
  Here every figure does, and a pre-2026-08-22 P&L may not be asserted as a target
  without an `explained_delta` naming what moved it.

  **`pinned_by`.** A rule with no test is unenforced; a test with no rule is undocumented
  behaviour. The registry names the test that pins each case.

  **`doc_quotes`.** Verbatim substrings of `l2-mechanisms.md`. If a figure is edited in
  the document alone, the quote stops matching and `missing_quotes()` reports it.

BOUNDARY, held deliberately: this registry carries WHAT was measured. It must never
carry HOW the Executor stores facts — implementation changes far faster than rules, and
that is how a specification rots.

**EVERY P&L FIGURE IN THIS REGISTRY PREDATES PLAN 16 (2026-09-13) AND WAS MEASURED WITH
THE 09:20 DOL AS THE TAKE-PROFIT.** That level is now inert: the take-profit is the T2
pick made at the entry fill, and it is also what kills the plan. The recorded numbers stay
exactly as they are — this registry is the record of what the documented walks measured,
and `doc_quotes` ties each one to `l2-mechanisms.md` — but under the `t2-target-20260913`
era a documented P&L is NOT a target the engine should reproduce. Two distinct reasons,
and a case can carry either or both:

  * **a different target level.** T2 is nearest-first over `build_menus`'s rows; the
    documented DOLs were hand-picked from the §10/§11 walks. 08-18 is the worked example:
    T2 takes `prev4_day_low` 29625.0 for +138.25 where the documented walk took
    `prev1_week_low` 29533.5 for +229.75. Neither is wrong — they are different selectors,
    and `l2-target-selection.md` §3 measures the SELECTOR question (T1 vs T2), not this one.
  * **a different death level.** The plan now ends on the T2 target rather than on the DOL,
    so attempt counts after the first target touch are not comparable either.

Do not "fix" a figure here to make a test pass. Either the test asserts the new behaviour
explicitly (naming plan 16), or the case is re-measured and given a new era entry.

Plan 42 (2026-09-27) did the latter for the §5/§10 named days: the `current-20260927`
records (keys `cur-*`, `sec5-0818-5m-t2`) are the live expectations, each with the chain of
commits that moved it; the records they replace are kept and marked SUPERSEDED.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "l2-mechanisms.md")

# ---------------------------------------------------------------------------- #
# Eras. The knob set a figure was recorded under, newest last.
# ---------------------------------------------------------------------------- #

#: era key -> (adoption date, what the era's knobs were)
ERAS = {
    "pre-veto": ("2026-08-15", "before the §2 DOL-floor veto and the §8 takeover"),
    "buffer7-h35": ("2026-08-19",
                    "entry buffer 7, structural stop-loss, max FVG height 35"),
    "adopted-20260822": ("2026-08-22",
                         "entry buffer 3, stop-loss cap 25, max FVG height 45"),
    "age-anchor-20260826": ("2026-08-26",
                            "adopted-20260822 knobs plus §7's AGE-keyed stale-extreme "
                            "anchor and §5's FRESH-tick retrace"),
    "t2-target-20260913": ("2026-09-13",
                           "age-anchor knobs, but the TAKE-PROFIT is no longer the 09:20 "
                           "DOL: plan 16 moved target selection to the entry fill (T2, "
                           "`agent/trader/target.py`), made the DOL inert (no floor veto, "
                           "no plan death) and made the T2 target the plan-death level"),
    "current-20260927": ("2026-09-27",
                         "t2-target plus §6/§7 wired to the bar loop (d349a05), "
                         "`tmso_reject` (de59b3b, f73c20f, 8b90ee7), the 5m-FVG entry "
                         "mechanisms suspended by default (879032b, ACT_TRADER_5M=1 "
                         "re-arms), HTF extremes as T2 candidates (2da4d0b) and "
                         "micro-session SMT O3/O4 (5f71acc). Plan 42 re-measured the "
                         "named days under it (l2-mechanisms.md §11.3)"),
}

#: The knob adoption that supersedes every earlier P&L. Anything recorded before this
#: needs an explained delta before it may be asserted as a target.
CURRENT_ERA = "current-20260927"
KNOB_ADOPTION_DATE = "2026-08-22"

#: The one delta this cycle has already accounted for, kept as a named constant because
#: it is the worked precedent the whole validation doctrine rests on: 08-13's documented
#: +89.25 becomes +93.25, a difference of exactly 4.00 pts — the entry-buffer 7→3 trim.
ENTRY_BUFFER_TRIM_PTS = 4.00


@dataclass(frozen=True)
class NamedCase:
    """One documented outcome, with everything needed to assert or to explain it."""

    key: str
    date: str
    mechanism: str
    doc_section: str
    era: str
    pinned_by: "tuple[str, ...]"
    #: "entry" | "flat" | "no_fire" | "no_takeover" | "strict_track"
    expect: str = "entry"
    entry_price: "float | None" = None
    entry_time: "str | None" = None
    exit_price: "float | None" = None
    exit_time: "str | None" = None
    stop: "float | None" = None
    pnl: "float | None" = None
    attempts_used: "int | None" = None
    #: The BOUND ARTIFACT. Asserting P&L alone is insufficient — the phase-1 ladder
    #: defect produced BETTER P&L (+130 vs +93) from the WRONG gap, and only inspecting
    #: which artifact was bound revealed it.
    artifact: "str | None" = None
    #: Why this figure differs from an earlier recorded one, or from the era's own.
    explained_delta: "str | None" = None
    #: Set when the case must NOT be used as a target, with the reason.
    excluded: "str | None" = None
    doc_quotes: "tuple[str, ...]" = field(default_factory=tuple)
    note: str = ""


CASES: "tuple[NamedCase, ...]" = (

    # ---- §5 `fvg_return_continuation`, the FRESH-tick resolution (§11) ---------- #
    NamedCase(
        key="sec5-0821-flat", date="2026-08-21", mechanism="fvg_return_continuation",
        doc_section="§11 §5 pre-arm penetration", era="age-anchor-20260826",
        expect="flat",
        pinned_by=("agent/trader/test_named_cases.py::"
                   "test_every_superseded_case_names_its_current_era_replacement",),
        excluded="SUPERSEDED 2026-09-27 by cur-0821: the day now trades (tmso_reject 09:31:00, +73.00); §11.3 attributes each step",
        note="The gap [29472.25, 29485.00] is penetrated BEFORE the window ends and "
             "never re-entered fresh. LITERAL fills 29442.50 for +169.00; FRESH takes "
             "nothing. FLAT is correct.",
        doc_quotes=("**08-21 must be FLAT** (prior penetration only, no fresh "
                    "re-entry)",),
    ),
    NamedCase(
        key="sec5-0811-flat", date="2026-08-11", mechanism="fvg_return_continuation",
        doc_section="§10 / §11 §5", era="age-anchor-20260826", expect="flat",
        pinned_by=("agent/trader/test_named_cases.py::"
                   "test_every_superseded_case_names_its_current_era_replacement",),
        excluded="SUPERSEDED 2026-09-27 by cur-0811: the day now trades (tmso_reject 09:31:00, +112.75); §11.3 attributes each step",
        note="§10's own accepted skip: the correct binding [29835.00, 29855.25] was "
             "only ever entered at or before the L1 arm.",
        doc_quotes=("**08-11 must be FLAT** (the §10 accepted skip)",),
    ),
    NamedCase(
        key="sec5-0818-fresh-entry", date="2026-08-18",
        mechanism="fvg_return_continuation", doc_section="§11 §5 per-day evidence",
        era="adopted-20260822",
        entry_time="09:40:58", entry_price=29763.25, pnl=229.75, attempts_used=1,
        artifact="MNQ 5min bear FVG",
        pinned_by=("agent/trader/test_named_sec5.py::"
                   "test_0818_enters_on_its_fresh_tick_not_at_the_window_end",
                   "agent/trader/test_named_sec5.py::"
                   "test_0818_binds_the_expected_artifact"),
        explained_delta="The §11 calibrated grid records +225.75 for this binding at "
                        "the buffer-7 knobs; the FRESH column at buffer 3 gives "
                        "+229.75 — exactly the 4.00-pt entry-buffer trim. The P&L was "
                        "NOT reproducible while `replay.py` ended at 11:00 and the DOL "
                        "is touched at 11:01:26. Since the window moved to 13:00 "
                        "(2026-09-09) the replay books it: take_profit 29533.5 at "
                        "11:01:26 for +229.75, to the cent. Entry, exit and P&L were all "
                        "exact until plan 16 (000dfb7): the ENTRY still reproduces to the "
                        "second under ACT_TRADER_5M=1 (the §5 mechanism is suspended by "
                        "default since 879032b), but the exit is now the T2 pick — see "
                        "`sec5-0818-5m-t2`.",
        doc_quotes=("**08-18 must enter 09:40:58 at 29763.25**",
                    "09:40:58 fill 29763.25 TP **+229.75**"),
    ),

    # ---- §7 `extreme_reject_close`, the AGE-keyed stale-extreme anchor ---------- #
    NamedCase(
        key="sec7-0818", date="2026-08-18", mechanism="extreme_reject_close",
        doc_section="§7 / §11 §7 named tests", era="age-anchor-20260826",
        entry_time="09:42:00", entry_price=29760.25,
        exit_time="11:01:26", exit_price=29533.5, pnl=226.75, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec7.py::"
                   "test_0818_fires_once_the_days_only_entry",),
        note="Counter-thesis 24h extreme 12.65h old at the arm -> fallback ACTIVE; the "
             "day's ONLY entry.",
        doc_quotes=("29760.25 at 09:42:00 → TP prev1_week_low 29533.5 at 11:01:26, "
                    "+226.75 — the day's ONLY",),
    ),
    NamedCase(
        key="sec7-0825", date="2026-08-25", mechanism="extreme_reject_close",
        doc_section="§7 / §11 §7 named tests", era="age-anchor-20260826",
        entry_time="09:50:00", entry_price=29392.75, stop=29407.75,
        exit_time="10:23:21", exit_price=29218.38, pnl=174.37, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec7.py::"
                   "test_0825_fires_and_is_the_case_that_retired_the_distance_key",),
        note="Extreme 3.55h old -> fallback ACTIVE. The case that retired the >150 pt "
             "distance key: 08-07 (152.50) and 08-25 (123.50) are ADJACENT in distance "
             "and need OPPOSITE answers; their ages (0.60h / 3.55h) separate cleanly.",
        doc_quotes=("market short 29392.75 at 09:50:00 off",
                    "10:23:21, +174.37 — the day's ONLY entry, and the case that "
                    "retired the distance key"),
    ),
    NamedCase(
        key="sec7-0807-strict", date="2026-08-07", mechanism="extreme_reject_close",
        doc_section="§7 / §11 §7 named tests", era="age-anchor-20260826",
        expect="strict_track",
        pinned_by=("agent/trader/test_extreme_reject.py::"
                   "test_a_fresh_extreme_keeps_the_strict_24h_track",
                   "agent/trader/test_named_sec7.py::"
                   "test_0807_must_not_activate_the_fallback"),
        note="Extreme 0.60h old at the arm. The documented wrong-arm guard: the strict "
             "24h rule governs and §5's validated ride is untouched.",
        doc_quotes=("08-07 (extreme 0.60h old — must NOT activate",),
    ),

    # ---- §6.2's CURRENT-rules column (never the recorded one) ------------------- #
    NamedCase(
        key="sec6-0805-current", date="2026-08-05", mechanism="fvg_1m_post_extreme",
        doc_section="§6.2", era="age-anchor-20260826",
        pnl=235.38, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec6.py::"
                   "test_0805_is_registered_as_unreproducible_with_its_reason",),
        explained_delta="Recorded +237.50 under max height 35 with no DOL-floor veto; "
                        "the 2.12-pt difference is the market-fill convention (1s mid "
                        "at placement).",
        doc_quotes=("| 08-05 | +237.50, one entry | max height 35, no DOL-floor veto | "
                    "**+235.38, one entry**",),
    ),
    NamedCase(
        key="sec6-0810-no-fire", date="2026-08-10", mechanism="fvg_1m_post_extreme",
        doc_section="§6.2", era="age-anchor-20260826", expect="no_fire",
        pinned_by=("agent/trader/test_named_sec6.py::test_0810_does_not_fire_at_all",),
        explained_delta="Recorded +34.25 on three entries PRE-VETO. The 09:51/09:59 "
                        "chase entries are suppressed by the §2 DOL-floor veto at 45.5 "
                        "pts remaining, and the day belongs to §7.",
        doc_quotes=("| 08-10 | +34.25, three entries | pre-veto | **no fire**",),
    ),
    NamedCase(
        key="sec6-0806-current", date="2026-08-06", mechanism="fvg_1m_post_extreme",
        doc_section="§6.2", era="age-anchor-20260826",
        pnl=125.12, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec6.py::"
                   "test_0806_adds_the_negative_cycle_the_height_raise_admits",),
        explained_delta="Recorded +145.75 under max height 35. At max height 45 the "
                        "36.75-pt gap [29375.25, 29412.00] becomes a candidate and adds "
                        "a -26.12 cycle the max-35 era never saw. This is §6.2's "
                        "sharpest lesson: a knob change dated AFTER a validation "
                        "silently invalidates that validation's entry set.",
        doc_quotes=("| 08-06 | +145.75, one entry | **max height 35** | one entry, "
                    "+125.12",),
    ),
    NamedCase(
        key="sec6-0721-current", date="2026-07-21", mechanism="fvg_1m_post_extreme",
        doc_section="§6.2", era="age-anchor-20260826",
        pnl=61.00, attempts_used=3,
        pinned_by=("agent/trader/test_named_sec6.py::"
                   "test_0721_within_one_cycle_of_the_current_rules_figure",),
        explained_delta="Recorded +96.50 on two entries under max height 35, pre-veto. "
                        "The runaway leg is EXACT (29149.75, -30.00). One extra "
                        "close-verdict cycle at 09:38:00 is unexplained by any stated "
                        "rule and is an ACCEPTED TOLERANCE — the doc says treat a "
                        "reproduction within one cycle as passing. Do NOT chase it.",
        doc_quotes=("| 07-21 | +96.50, two entries | max height 35, pre-veto | +61.00, "
                    "three entries",),
    ),

    # ---- §8 deeper-gap takeover ------------------------------------------------- #
    NamedCase(
        key="sec8-0721-takeover", date="2026-07-21", mechanism="fvg_takeover",
        doc_section="§11 takeover scanner", era="age-anchor-20260826",
        entry_time="09:35:00", pnl=-42.25, attempts_used=2,
        artifact="1m gap [29137.25, 29138.5]",
        pinned_by=("agent/trader/test_named_sec8.py::"
                   "test_0721_the_1m_gap_takes_over_at_the_stop_tick",),
        note="The takeover gap is a 1m gap. Two independent manual walks scanned only "
             "5m and produced a wrong ledger — this is the named failure Task 2 exists "
             "for. Attempt 3 is UNSPENT.",
        doc_quotes=("07-21 (1m gap [29137.25, 29138.5] takes over at the 09:33:58 stop "
                    "tick",),
    ),
    NamedCase(
        key="sec8-0731-deepest", date="2026-07-31", mechanism="fvg_takeover",
        doc_section="§11 takeover scanner / §10.2", era="age-anchor-20260826",
        entry_time="09:44:00", pnl=-67.50, attempts_used=3,
        artifact="1m gap [28455.75, 28477]",
        pinned_by=("agent/trader/test_named_sec8.py::"
                   "test_0731_binds_the_deepest_penetrated_gap",),
        note="Binds the DEEPEST eligible penetrated gap — the 1m [28455.75, 28477], "
             "not the shallower 5m [28496.75, 28508.5].",
        doc_quotes=("eligible penetrated gap [28455.75, 28477], not the shallower 5m — "
                    "exit-tick 09:44:00",),
    ),
    NamedCase(
        key="sec8-0812-no-takeover", date="2026-08-12", mechanism="fvg_takeover",
        doc_section="§11 takeover scanner", era="age-anchor-20260826",
        expect="no_takeover",
        pinned_by=("agent/trader/test_named_sec8.py::"
                   "test_a_close_through_dead_candidate_is_never_a_takeover",),
        note="Deeper 1m candidates exist but are close-through DEAD -> no takeover.",
        doc_quotes=("08-12 and 08-05 (deeper 1m candidates exist but are close-through",),
    ),
    NamedCase(
        key="sec8-0805-no-takeover", date="2026-08-05", mechanism="fvg_takeover",
        doc_section="§11 takeover scanner", era="age-anchor-20260826",
        expect="no_takeover",
        pinned_by=("agent/trader/test_named_sec8.py::"
                   "test_a_close_through_dead_candidate_is_never_a_takeover",),
        doc_quotes=("DEAD → no takeover, validated records unchanged",),
    ),
    NamedCase(
        key="sec8-0724-crossed", date="2026-07-24", mechanism="fvg_takeover",
        doc_section="§8 crossed-trigger precedence", era="age-anchor-20260826",
        entry_time="09:36:00", entry_price=28579.0, pnl=146.5,
        artifact="the FAILED §5 binding (the takeover does NOT divert)",
        pinned_by=("agent/trader/test_named_sec8.py::"
                   "test_0724_crossed_trigger_precedence_preserves_the_collapse_"
                   "capture",),
        note="Two eligible deeper 1m gaps penetrated, but the FAILED binding's trigger "
             "is already crossed at the cooldown end -> §2 market execution takes "
             "precedence and the takeover does NOT divert. The episode's SL-cap gate "
             "would have SKIPPED this at 45.25 pts, recreating the exact lockout the "
             "cooldown rule was built to kill.",
        doc_quotes=("crossed trigger at cooldown end takes precedence → market re-entry"
                    "\n  28579, +146.5 preserved",),
    ),
    NamedCase(
        key="sec8-0814-three-attempts", date="2026-08-14", mechanism="fvg_takeover",
        doc_section="§8 / §10", era="age-anchor-20260826",
        entry_time="09:32:00", entry_price=30245.5, stop=30268.0,
        pnl=99.50, attempts_used=3,
        pinned_by=("agent/trader/test_named_sec8.py::"
                   "test_0814_market_at_the_1s_mid_and_exactly_three_attempts",),
        note="Crossed trigger at 09:32:00 -> market at the 1s mid. Assert the attempt "
             "count; it contributes NOTHING to the P&L arithmetic but is a sharper "
             "change detector than P&L.",
        doc_quotes=("08-14 (crossed trigger at 09:32:00 → market 30245.5, day",),
    ),

    # ---- §10's forward test — the ONLY multi-mechanism record ------------------- #
    NamedCase(
        key="sec10-0811-no-entry", date="2026-08-11", mechanism="multi",
        doc_section="§10", era="buffer7-h35", expect="flat",
        pinned_by=("agent/trader/test_named_cases.py::"
                   "test_every_superseded_case_names_its_current_era_replacement",),
        excluded="SUPERSEDED 2026-09-27 by cur-0811: the day now trades (tmso_reject 09:31:00, +112.75); §11.3 attributes each step",
        explained_delta="Recorded under the max-height-35 / buffer-7 era. The verdict "
                        "(no entry) is era-invariant: no adverse day extreme printed, "
                        "so §6/§7 cannot arm, and §5's binding was only entered at or "
                        "before the arm.",
        doc_quotes=("**08-11 (down, DOL 29666 = overnight low): no entry — accepted "
                    "skip.**",),
    ),
    NamedCase(
        key="sec10-0812", date="2026-08-12", mechanism="multi",
        doc_section="§10", era="buffer7-h35", pnl=46.75, attempts_used=2,
        pinned_by=("agent/trader/test_named_cases.py::"
                   "test_every_superseded_case_names_its_current_era_replacement",),
        excluded="SUPERSEDED 2026-09-27 by cur-0812: tmso_reject confirms on its sweep bar at 09:31:00 (f73c20f) and pre-empts the §4 negation fill at 09:31:16 even with ACT_TRADER_5M=1; the §4 shape is unreachable in this era",
        explained_delta="Recorded at buffer 7. Expect an entry-price difference of the "
                        "4.00-pt buffer trim on each of the two attempts; the SHAPE "
                        "(negation stop-out, then the widened 1m fallback with its "
                        "first candidate DOL-floor-vetoed at 57.25 and its second "
                        "allowed at 77.75) is the assertion.",
        doc_quotes=("- **08-12 (down, DOL 29842.75 = prev RTH high): +46.75.**",),
    ),
    NamedCase(
        key="sec10-0813", date="2026-08-13", mechanism="fvg_return_continuation",
        doc_section="§10", era="buffer7-h35", pnl=89.25, attempts_used=1,
        entry_time="09:33:26", entry_price=29908.25,
        exit_time="09:36:43", exit_price=30001.5,
        artifact="MNQ 5min bull FVG 08-13 09:00 [29881.5, 29905.25]",
        pinned_by=("agent/trader/test_arbiter_forward.py::"
                   "test_0813_binds_the_5m_continuation_and_no_other_mechanism_"
                   "preempts_it",),
        explained_delta="The worked precedent for the whole validation doctrine: the "
                        "documented +89.25 is a buffer-7-era number and the correct "
                        "current answer is +93.25 — a difference of exactly 4.00 pts, "
                        "the adopted 7->3 entry-buffer trim. Measured in phases 0+1 and "
                        "re-measured unchanged after existence gating landed.",
        doc_quotes=("- **08-13 (up, DOL 30001.5 = prev RTH high): +89.25.**",),
    ),
    NamedCase(
        key="sec10-0814", date="2026-08-14", mechanism="multi",
        doc_section="§10", era="age-anchor-20260826", pnl=99.50, attempts_used=3,
        pinned_by=("agent/trader/test_named_cases.py::"
                   "test_every_superseded_case_names_its_current_era_replacement",),
        excluded="SUPERSEDED 2026-09-27 by cur-0814: tmso_reject takes attempt 1 and §6 the winner (+38.50, 2 att); §11.3",
        note="1s-verified under the §8 takeover with crossed-trigger precedence. The "
             "shared 3-attempt counter is spent across DIFFERENT mechanisms here.",
        doc_quotes=("- **08-14 (down, DOL 30124.25 = overnight low): +99.50**",),
    ),

    # ---- §11.3 current-era re-measurement (plan 42, 2026-09-27) ---------------- #
    # Default configuration (5m-FVG mechanisms suspended), oracle theses, cb083e2.
    NamedCase(
        key="cur-0811", date="2026-08-11", mechanism="tmso_reject",
        doc_section="§11.3", era="current-20260927",
        entry_time="09:31:00", entry_price=29778.75,
        exit_time="09:46:01", exit_price=29666.0, pnl=112.75, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec5.py::"
                   "test_0811_current_era_takes_tmso_reject",
                   "agent/trader/test_arbiter_forward.py::test_0811_current_era_shape"),
        explained_delta="Documented FLAT. 000dfb7 +99.25 (DOL inert: the day no longer "
                        "ends at its touch) -> d349a05 -59.50 (§6 wired) -> de59b3b "
                        "+101.00 (tmso_reject) -> f73c20f +112.75 (sweep bar confirms).",
        doc_quotes=("08-11 current: `tmso_reject` DOWN 09:31:00 @ 29778.75, TP 09:46:01, "
                    "**+112.75**, 1 att",),
    ),
    NamedCase(
        key="cur-0812", date="2026-08-12", mechanism="tmso_reject",
        doc_section="§11.3", era="current-20260927",
        entry_time="09:31:00", entry_price=29974.5, pnl=69.00, attempts_used=1,
        pinned_by=("agent/trader/test_arbiter_forward.py::test_0812_current_era_shape",),
        explained_delta="Documented +46.75 on two §4 attempts. 000dfb7 -6.00 (T2 exit, "
                        "the winner is marked) -> 879032b -52.50 (5m suspended) -> "
                        "f73c20f +69.00: tmso_reject enters 09:31:00 and is MARKED at "
                        "12:59:59 (no T2 touch in the window).",
        doc_quotes=("08-12 current: `tmso_reject` DOWN 09:31:00 @ 29974.5, marked "
                    "12:59:59, **+69.00**, 1 att",),
    ),
    NamedCase(
        key="cur-0813", date="2026-08-13", mechanism="tmso_reject",
        doc_section="§11.3", era="current-20260927",
        entry_time="09:35:00", entry_price=29945.75,
        exit_time="09:36:43", exit_price=30001.5, pnl=55.75, attempts_used=1,
        pinned_by=("agent/trader/test_arbiter_forward.py::test_0813_current_era_shape",),
        explained_delta="Documented +89.25 (= +93.25 at buffer 3) via the 5m continuation "
                        "09:33:26, which 879032b suspends (+93.50 via tmso_reject 09:36); "
                        "f73c20f's sweep-bar confirmation enters 09:35:00 at a worse "
                        "price to the SAME target 30001.5 at the same 09:36:43: +55.75.",
        doc_quotes=("08-13 current: `tmso_reject` UP 09:35:00 @ 29945.75, TP 09:36:43, "
                    "**+55.75**, 1 att",),
    ),
    NamedCase(
        key="cur-0814", date="2026-08-14", mechanism="multi",
        doc_section="§11.3", era="current-20260927", pnl=38.50, attempts_used=2,
        pinned_by=("agent/trader/test_arbiter_forward.py::test_0814_current_era_shape",),
        explained_delta="Documented +99.50 on 3 attempts (a shape that never reproduced, "
                        "see sec10-0814). 000dfb7 +68.50 -> de59b3b +62.50 -> 879032b "
                        "+38.50: tmso_reject 09:32:00 stops -15.00, then "
                        "fvg_1m_post_extreme 10:22:00 @ 30181.5 takes T2 at 10:59:23 "
                        "+53.50.",
        doc_quotes=("08-14 current: `tmso_reject` -15.00 then `fvg_1m_post_extreme` "
                    "10:22:00 @ 30181.5, TP 10:59:23, **+38.50**, 2 att",),
    ),
    NamedCase(
        key="cur-0818", date="2026-08-18", mechanism="extreme_reject_close",
        doc_section="§11.3", era="current-20260927",
        entry_time="09:42:00", entry_price=29760.25,
        exit_time="09:55:08", exit_price=29631.0, pnl=129.25, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec7.py::test_0818_current_era_day_is_this_entry",),
        explained_delta="Documented §5 09:40:58 +229.75. With 5m suspended (879032b) the "
                        "day's entry is §7's own 09:42:00 @ 29760.25 (sec7-0818's entry, "
                        "to the second); its exit is the T2 pick (000dfb7), since plan 40 "
                        "(2da4d0b) htf_week_running_low 29631.0. 8b90ee7's "
                        "veto removes an earlier losing tmso_reject attempt (5f71acc "
                        "+114.25 -> +129.25).",
        doc_quotes=("08-18 current: `extreme_reject_close` DOWN 09:42:00 @ 29760.25, TP "
                    "09:55:08, **+129.25**, 1 att",),
    ),
    NamedCase(
        key="sec5-0818-5m-t2", date="2026-08-18", mechanism="fvg_return_continuation",
        doc_section="§11.3", era="current-20260927",
        entry_time="09:40:58", entry_price=29763.25,
        exit_time="09:55:08", exit_price=29631.0, pnl=132.25, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec5.py::"
                   "test_0818s_exit_is_the_t2_pick_under_plan_16",),
        explained_delta="The §5 mechanism under ACT_TRADER_5M=1: the documented entry to "
                        "the second. Exit: the DOL at 11:01:26 (+229.75) -> the T2 pick "
                        "prev4_day_low 29625.0 (plan 16, 000dfb7, +138.25) -> plan 40's "
                        "nearer htf_week_running_low 29631.0 at 09:55:08 (2da4d0b, "
                        "+132.25).",
        doc_quotes=("now exiting at the T2 pick `htf_week_running_low` 29631.0 at 09:55:08 "
                    "for +132.25",),
    ),
    NamedCase(
        key="cur-0821", date="2026-08-21", mechanism="tmso_reject",
        doc_section="§11.3", era="current-20260927",
        entry_time="09:31:00", entry_price=29425.75,
        exit_time="09:35:13", exit_price=29352.75, pnl=73.00, attempts_used=1,
        pinned_by=("agent/trader/test_named_sec5.py::"
                   "test_0821_current_era_takes_tmso_reject",),
        explained_delta="Documented FLAT. 000dfb7 +19.25 -> d349a05 +87.00 (§6) -> "
                        "de59b3b +80.25 (tmso_reject) -> f73c20f +73.00.",
        doc_quotes=("08-21 current: `tmso_reject` DOWN 09:31:00 @ 29425.75, TP 09:35:13, "
                    "**+73.00**, 1 att",),
    ),

    # ---- §7a.1 the pre-open micro-SMT pair (operator, 2026-09-28) --------------- #
    NamedCase(
        key="sec7a1-0928", date="2026-09-28", mechanism="micro_smt_reject",
        doc_section="§7a.1", era="current-20260927",
        entry_time="09:34:00", entry_price=30707.75, stop=30722.75,
        exit_time="10:11:14", exit_price=30535.0, pnl=172.75, attempts_used=1,
        pinned_by=("agent/trader/test_micro_smt.py::"
                   "test_0928_real_tape_preopen_pair_fires_at_0934",
                   "agent/trader/test_micro_smt.py::"
                   "test_0928_counterfactual_down_replay_is_this_one_trade"),
        note="The fire (entry/stop) is thesis-independent and pinned over the real 1m "
             "tape. The exit and P&L come from a COUNTERFACTUAL DOWN thesis (the 09:20 "
             "DOWN-menu D1 london(cur)_low 30535.0, falsifier F1). Under the live UP thesis "
             "this bearish fire is not taken, but the bullish mirror fires 09:37:00 @ "
             "30658.0 (-15.00) and the replayed day moves -58.75 -> -45.00 (§7a.1). "
             "Recorded in the current era: under the first-in-window-confirmation latch "
             "(operator, 2026-09-29) §7a.1 moves none of plan 42's current-era figures "
             "(plan 43's named-case check; without the latch cur-0814 moved -15.00).",
        doc_quotes=("fires at 09:34:00, short @ 30707.75, stop",
                    "min(30759.00 + 2, 30707.75 + 15) = 30722.75",
                    "T2 = 30535.00, reached",
                    "10:11:14: **+172.75 pts, the day's only trade.**"),
    ),

    # ---- §2 the extension veto (operator, 2026-09-29) --------------------------- #
    NamedCase(
        key="sec2-0929-extension", date="2026-09-29", mechanism="fvg_1m_post_extreme",
        doc_section="§2 extension veto", era="current-20260927", expect="no_fire",
        pinned_by=("agent/trader/test_executor_extension_veto.py::"
                   "test_down_plan_173_pts_from_the_post_open_high_is_vetoed_with_"
                   "full_detail",
                   "agent/trader/test_executor_extension_veto.py::"
                   "test_0929_real_tape_the_0944_short_is_vetoed"),
        note="The recorded live thesis (DOWN, DOL TDO 30443.75, F1 30827.5). The 09:31:00 "
             "micro_smt_reject short @ 30675.75 is 49.25 pts below the post-09:30 high "
             "30725.00 (09:30:02) and is taken (stopped 09:31:14 @ 30690.75, -15.00); "
             "the 09:44:00 fvg_1m_post_extreme short @ 30552.00 is 173.00 below it and "
             "is VETOED (it was -3.50). Asserted up to 09:44:00 only: the tape recorded "
             "when this was written ends 10:34, so no day figure is registered. "
             "Measured with §7c's stop-bar retry OFF (2026-09-30): with it ON (the "
             "default) the 09:31 stop-out's retry (09:32:00 @ 30661.25, "
             "sec7c-0929-retry) is still open at 09:44, so the 09:44:00 fire is never "
             "produced and there is nothing to veto; the pinning test runs both.",
        doc_quotes=("| 09:31:00 `micro_smt_reject` DOWN | 30675.75 | 30725.00 | 49.25 | "
                    "allowed |",
                    "| 09:44:00 `fvg_1m_post_extreme` DOWN | 30552.00 | 30725.00 | "
                    "173.00 | VETOED |"),
    ),

    # ---- §7c the stop-bar retry (operator, 2026-09-30) -------------------------- #
    NamedCase(
        key="sec7c-0929-retry", date="2026-09-29", mechanism="micro_smt_reject",
        doc_section="§7c", era="current-20260927",
        entry_time="09:32:00", entry_price=30661.25, stop=30676.25,
        exit_time="10:25:57", exit_price=30676.25, pnl=-15.00, attempts_used=2,
        pinned_by=("agent/trader/test_executor_stop_bar_retry.py::"
                   "test_0929_real_tape_the_retry_is_taken_and_stopped_at_102557",),
        note="The recorded live thesis (sec2-0929-extension's). The 09:31:00 "
             "micro_smt_reject short @ 30675.75 is stopped 09:31:14 @ 30690.75 inside a "
             "bar that then closes 30676.75 -> 30661.25 (MES 7750.50 -> 7749.00): the "
             "retry fires at 09:32:00 @ 30661.25, stop min(30705.75 + 2, 30661.25 + 15) "
             "= 30676.25, survives the 09:32 high 30676.00 by one tick, reaches the "
             "day-mid initial target (09:44) and, under the T2-only exit (TDO "
             "30443.75 never reached), is stopped 10:25:57 for -15.00. Day -15.00 -> "
             "-30.00 on 2 attempts; the 09:44:00 fvg_1m_post_extreme fire that "
             "sec2-0929-extension records as VETOED is never produced (a position is "
             "open). The tape ends 10:34, so the figure is asserted up to 10:30 only.",
        doc_quotes=("retry 09:32:00 @ 30661.25, stop 30676.25",
                    "stopped 10:25:57 @ 30676.25, **-15.00**; day -15.00 -> -30.00, 2 att"),
    ),

    # ---- §11's calibrated fill rows (the ten documented 5m bindings) ------------ #
    NamedCase(
        key="calib-0821-EXCLUDED", date="2026-08-21",
        mechanism="fvg_return_continuation", doc_section="§11 ERRATUM 2026-08-22",
        era="buffer7-h35", pnl=92.75, expect="flat",
        excluded="LOOK-AHEAD CONTAMINATED. That replay timestamped each FVG with its "
                 "third bar's LABEL instead of the bar's COMPLETION, so the 44-pt gap "
                 "[29373.25, 29417.25] became actionable up to five minutes before it "
                 "could be known. Its recorded 09:36:04 fill is impossible: the gap "
                 "exists only at 09:40:00, and between real creation and the 09:54:07 "
                 "DOL touch price never ticks back into it (max high 29361.50 vs the "
                 "29373.25 bottom). NOT DELETED — kept with its reason, so a future "
                 "agent cannot re-derive it.",
        pinned_by=("agent/trader/test_named_cases.py::"
                   "test_the_0821_fill_row_is_registered_as_EXCLUDED_with_its_reason",),
        doc_quotes=("the 08-21 row above is LOOK-AHEAD CONTAMINATED and its\n  +92.75 "
                    "is not attainable.",),
    ),
)

#: The nine calibrated 5m-binding P&Ls that are NOT contaminated, buffer-7 era. Kept as
#: a flat mapping rather than nine records: they are one measurement (the 2026-08-22
#: calibration sweep), and each is superseded by the same 4.00-pt buffer trim.
CALIBRATED_BUFFER7_FILLS = {
    "2026-07-17": -17.75, "2026-07-21": -30.75, "2026-07-31": -24.75,
    "2026-08-07": 198.00, "2026-08-12": -31.00, "2026-08-13": 89.25,
    "2026-08-14": -15.75, "2026-08-17": 77.00, "2026-08-18": 225.75,
}
CALIBRATED_ERA = "buffer7-h35"
CALIBRATED_DOC_QUOTE = (
    "(07-17 −17.75,\n  07-21 −30.75, 07-31 −24.75, 08-07 +198, 08-12 −31, "
    "08-13 +89.25, 08-14 −15.75, 08-17 +77,\n  08-18 +225.75, 08-21 +92.75)")

#: Tolerances the document accepts explicitly. Reproducing them is NOT required and
#: chasing them is a defect — the plan says so and so does §6.2.
ACCEPTED_TOLERANCES = {
    "sec6-0806-current": "§6.1's doc-literal subsequent-bar gate differs from a raw "
                         "exit-tick reading by +/-1 cycle on 08-06. Doc-literal is the "
                         "specified reading; the discrepancy is a known uncertainty.",
    "sec6-0721-current": "07-21 carries one extra close-verdict cycle at 09:38:00 "
                         "(-27.88) unexplained by any stated rule. Treat a reproduction "
                         "within one cycle of these figures as passing.",
}


# ---------------------------------------------------------------------------- #
# Lookup and drift detection
# ---------------------------------------------------------------------------- #

def by_key(key: str) -> NamedCase:
    for c in CASES:
        if c.key == key:
            return c
    raise KeyError(key)


def for_date(date: str) -> "list[NamedCase]":
    return [c for c in CASES if c.date == date]


def read_doc(path: str = DOC) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def all_quotes() -> "list[tuple[str, str]]":
    """(case key, quote) for every figure the registry claims the document states."""
    out = [(c.key, q) for c in CASES for q in c.doc_quotes]
    out.append(("calibrated-buffer7-fills", CALIBRATED_DOC_QUOTE))
    return out


def missing_quotes(doc_text: "str | None" = None) -> "list[tuple[str, str]]":
    """Every (case key, quote) the document no longer contains.

    This is the drift detector. A figure edited in `l2-mechanisms.md` alone stops
    matching here, and the registry's own suite fails — instead of the divergence being
    discovered months later by a replay that quietly reproduces the wrong number.
    """
    text = doc_text if doc_text is not None else read_doc()
    return [(k, q) for k, q in all_quotes() if q not in text]


# ---------------------------------------------------------------------------- #
# Oracle theses — the L1 input each named case was recorded under
# ---------------------------------------------------------------------------- #
#
# Keyed by CASE, not by date, because two cases can share a date under different
# theses: §6.2's 07-21 row descends from §6's own validation (thesis DOWN, DOL = TDO
# 29072.50) while §8's 07-21 takeover case descends from §10.1's recorded UP plan with
# the far prev3_day_high DOL. Keying by date would silently merge them.
#
# `falsified_if` is REQUIRED by `validate_oracle_thesis`. (`exhausted_if` was removed
# 2026-08-29 — reaching the DOL *is* the exhaustion, and every recorded thesis set it to
# exactly the DOL price, so it only duplicated the DOL touch.)
#
# HONESTY NOTE. The document records each day's DIRECTION and DOL but never its
# `valid_while` predicates. Where a DOL is stated it is used verbatim; where it is only
# derivable from a recorded entry and P&L, the derivation is written out beside it. The
# falsifiers are NOT documented anywhere, so they are set deliberately far — beyond any
# excursion the day made — and the plans therefore die at their DOL or on the attempt
# budget, which is how every documented walk in fact ended. A tighter falsifier would be
# an invented rule, not a reproduction.

def _thesis(bias, dol_level, dol_price, *, falsify_at, regime="TREND",
            confidence="MEDIUM"):
    side_out = "above" if bias == "DOWN" else "below"
    return {
        "bias": bias, "regime": regime, "confidence": confidence,
        "dol": {"level": dol_level, "price": dol_price},
        "falsified_if": [{"type": "price_beyond", "price": falsify_at,
                          "side": side_out}],
    }


ORACLE_THESES = {
    # -- §5 fresh-tick set ---------------------------------------------------- #
    # 08-21's DOL is derived: §11's LITERAL row fills 29442.50 and takes +169.00, and the
    # erratum names the touch instant (09:54:07). 29442.50 - 169.00 = 29273.50.
    "sec5-0821-flat": _thesis("DOWN", "oracle_dol", 29273.50, falsify_at=29700.0),
    "sec5-0811-flat": _thesis("DOWN", "overnight_low", 29666.0, falsify_at=30050.0),
    "sec5-0818-fresh-entry": _thesis("DOWN", "prev1_week_low", 29533.5,
                                     falsify_at=29900.0),

    # -- §7 stale-extreme set ------------------------------------------------- #
    "sec7-0818": _thesis("DOWN", "prev1_week_low", 29533.5, falsify_at=29900.0),
    "sec7-0825": _thesis("DOWN", "daily_mid", 29218.38, falsify_at=29520.0),
    # 08-07's DOL is derived from §11's calibrated row: the §5 short filled 29798 at the
    # buffer-7 knobs and took +198.00. 29798 - 198 = 29600.
    "sec7-0807-strict": _thesis("DOWN", "oracle_dol", 29600.0, falsify_at=29950.0),

    # -- §6.2's CURRENT-rules column ------------------------------------------ #
    # 08-06's DOL is derived from §4's 1s-verified row: the negation long fills 29309.5
    # and takes +266.5 to the DOL. 29309.5 + 266.5 = 29576.
    "sec6-0806-current": _thesis("UP", "oracle_dol", 29576.0, falsify_at=29200.0),
    "sec6-0810-no-fire": _thesis("UP", "tdo", 29851.5, falsify_at=29600.0),
    "sec6-0721-current": _thesis("DOWN", "tdo", 29072.50, falsify_at=29500.0),

    # -- §8 takeover set ------------------------------------------------------ #
    # 07-21 and 07-31 are §10.1's RECORDED theses under the plan-18 DOL floor — both far
    # DOLs the day never reaches, which is exactly why the wrong plan stands and the
    # mechanisms trade it three times.
    "sec8-0721-takeover": _thesis("UP", "prev3_day_high", 29796.5, falsify_at=28900.0),
    "sec8-0731-deepest": _thesis("UP", "prev4_day_high", 28763.75, falsify_at=28100.0),
    # 07-24's DOL is derived from §8: market 28579 -> DOL TP +146.5 => 28432.50.
    "sec8-0724-crossed": _thesis("DOWN", "london_low", 28432.50, falsify_at=28900.0),
    "sec8-0814-three-attempts": _thesis("DOWN", "overnight_low", 30124.25,
                                        falsify_at=30450.0),
    "sec8-0812-no-takeover": _thesis("DOWN", "prev_rth_high", 29842.75,
                                     falsify_at=30100.0),

    # -- §10's forward test --------------------------------------------------- #
    "sec10-0811-no-entry": _thesis("DOWN", "overnight_low", 29666.0,
                                   falsify_at=30050.0),
    "sec10-0812": _thesis("DOWN", "prev_rth_high", 29842.75, falsify_at=30100.0),
    "sec10-0813": _thesis("UP", "prev_rth_high", 30001.5, falsify_at=29700.0),
    "sec10-0814": _thesis("DOWN", "overnight_low", 30124.25, falsify_at=30450.0),

    # -- §7a.1 --------------------------------------------------------------- #
    # NOT an oracle in the usual sense: the operator's COUNTERFACTUAL for 2026-09-28 (the
    # live thesis was UP). DOL and falsifier are the 09:20 DOWN menu's D1 and F1 exactly;
    # a far `price_beyond` falsifier would NOT be equivalent here -- MNQ prints 30759.00
    # at 09:32, above F1's price, and only F1's two-5m-close form survives it.
    "sec7a1-0928": {
        "bias": "DOWN", "regime": "HYBRID", "confidence": "MEDIUM",
        "dol": {"level": "london(cur)_low", "price": 30535.0},
        "falsified_if": [{"type": "n_closes_beyond", "price": 30727.88, "side": "above",
                          "tf": "5m", "n": 2}],
    },

    # -- §2 extension veto ---------------------------------------------------- #
    # The thesis the live session RECORDED on 2026-09-29 (its replay_thesis.json):
    # direction, DOL and falsifier verbatim.
    "sec2-0929-extension": _thesis("DOWN", "TDO", 30443.75, falsify_at=30827.5,
                                   regime="RANGE"),
    # -- §7c stop-bar retry: the same recorded 09-29 thesis ------------------ #
    "sec7c-0929-retry": _thesis("DOWN", "TDO", 30443.75, falsify_at=30827.5,
                                regime="RANGE"),
}

#: Cases the document does NOT give enough input to replay. Recorded rather than
#: silently omitted — an absent thesis is a validation GAP, not a passing case.
NO_ORACLE_THESIS = {
    "sec6-0805-current": "§6 records 08-05's outcome (+237.50 -> +235.38 under current "
                         "rules) but never its DOL, and no recorded entry price lets it "
                         "be derived. Direction is DOWN (§7's 08-05 row).",
    "sec8-0805-no-takeover": "Same day, same missing DOL.",
    "calib-0821-EXCLUDED": "Excluded by the erratum; not replayed as a target.",
    "sec7-0807-strict": None,          # has one; kept out of the gap list
}
NO_ORACLE_THESIS.pop("sec7-0807-strict")


#: Task 11 Step 4's list, verbatim: the attempt counts every named case documents.
#: Attempts are ASSERTED, never scored — they contribute NOTHING to the P&L arithmetic —
#: but attempts-used is a SHARPER change detector than P&L, because a stopped-out attempt
#: followed by a winner nets the same as one clean entry. A row reproducing the right P&L
#: on a different attempt count is a real discrepancy.
#:
#: Keyed by CASE, not by date, and the reason is 07-21: §6.2's CURRENT column records
#: THREE entries under §6's own DOWN-thesis study, while §10.1's recorded UP plan on the
#: same date ends "-42.25 with one attempt in reserve", i.e. two. Two theses, one date,
#: two correct counts — a date-keyed table silently asserts one against the other.
DOCUMENTED_ATTEMPTS = {
    "sec8-0814-three-attempts": 3,   # "day +99.50 on EXACTLY 3 attempts"
    "sec10-0814": 3,                 # the same day in §10's forward test
    "sec8-0731-deepest": 3,          # "day -67.50 on exactly 3 attempts"
    "sec8-0721-takeover": 2,         # "-42.25 with ONE ATTEMPT IN RESERVE" of the 3
    "sec6-0721-current": 3,          # §6.2's CURRENT column: "+61.00, three entries"
    "sec7-0818": 1,                  # "+226.75 on one attempt"
    "sec5-0818-fresh-entry": 1,      # the same entry, from §5's side
    "sec10-0812": 2,                 # negation stop-out, then the 1m fallback winner
    "sec6-0805-current": 1,          # "+235.38, one entry"
    "sec6-0806-current": 1,          # "one entry, +125.12"
    "sec7-0825": 1,                  # "+174.37 — the day's ONLY entry"
    "sec10-0813": 1,                 # §10: one clean §5 entry to the DOL
    # §11.3, current-20260927 (plan 42)
    "cur-0811": 1, "cur-0812": 1, "cur-0813": 1, "cur-0814": 2, "cur-0818": 1,
    "cur-0821": 1, "sec5-0818-5m-t2": 1,
    "sec7a1-0928": 1,                # §7a.1: "the day's only trade"
    "sec7c-0929-retry": 2,           # §7c: the stop-out and its retry, both stopped
}

#: Which of those counts this cycle can check END TO END, and which it cannot.
#: Everything not listed here is asserted against a replay in `test_arbiter_forward.py`
#: or `test_named_sec5.py`.
ATTEMPTS_NOT_DAY_CHECKABLE = {
    "sec8-0731-deepest": "The day's 3 attempts run through §8's episode-mode re-entry, "
                         "which is not on the Executor's entry path this cycle. The "
                         "takeover BINDING is reproduced exactly (test_named_sec8); the "
                         "attempt sequence is not.",
    "sec8-0721-takeover": "Same: the reserve attempt is unspent because every later "
                          "cycle colour-voids or is SL-cap skipped, both episode-path "
                          "behaviours.",
    "sec6-0721-current": "§6 is not on the Executor's entry path, so its three entries "
                         "cannot spend a real budget. The fire SEQUENCE is asserted.",
    "sec6-0805-current": "No recorded DOL; the day cannot be replayed at all.",
    "sec6-0806-current": "§6 not wired; the candidate-admission claim is asserted "
                         "instead.",
    "sec8-0814-three-attempts": "08-14's documented SHAPE does not reproduce — the "
                                "Planner arms §4, not §5 (see test_arbiter_forward). "
                                "The divergence is asserted explicitly.",
    "sec10-0814": "Same divergence.",
    "sec7-0818": "§7 is not wired into the Executor; the FIRE is reproduced exactly "
                 "over the tape instead.",
    "sec7-0825": "Same.",
}

#: Cases whose DAY-LEVEL outcome cannot be reproduced from what the document records.
#: Written down rather than quietly dropped: an unreproducible case is a VALIDATION GAP,
#: and a gap nobody wrote down becomes a claim nobody checked.
NO_DAY_REPRODUCTION = {
    "sec8-0812-no-takeover":
        "The verdict ('deeper 1m candidates exist but are close-through DEAD -> no "
        "takeover') is checkable only against the FAILED binding, and §10 records "
        "08-12's negation entry price (short 29933 at buffer 7) but never the bound "
        "gap's bounds. The RULE is pinned instead, at unit level, by "
        "test_named_sec8.py::test_a_close_through_dead_candidate_is_never_a_takeover.",
    "sec8-0805-no-takeover": "Same shape: 08-05's failed binding is described only by "
                             "its fill and stop, not by its bounds.",
    "sec6-0805-current": "No DOL recorded for 08-05 and none derivable — see "
                         "NO_ORACLE_THESIS.",
    # sec5-0818-fresh-entry WAS listed here — "the +229.75 does not reproduce, the DOL
    # is touched at 11:01:26 and the window ends at 11:00". The window moved to 13:00 on
    # 2026-09-09 and the replay now books +229.75 at 11:01:26 exactly, so the gap is
    # CLOSED and the entry is removed rather than reworded. Pinned by
    # test_named_sec5.py::test_0818s_exit_now_reproduces_inside_the_window.
    "sec7-0818": "§7 is not wired into the Executor, so no replay books this day's "
                 "outcome. (Until 2026-09-09 the stated reason was the 11:00 window "
                 "truncating the 11:01:26 TP; the window now reaches it, and this is "
                 "the reason that was underneath.)",
}


#: §10.2's wrong-thesis stress. Each forward-test day rerun with the thesis INVERTED and
#: a symmetric opposite-side DOL, to BOUND the bleed when L1 is wrong — the live edge
#: depends on that bound, and §10.2's conclusion is that every observed adverse day is
#: held to 0..-70 by three brakes: the DOL-floor veto on near-DOL chases, early sweeps of
#: wrong-direction DOLs completing plans flat, and the §6/§7/§8 colour/cycle/SL-cap gates.
#:
#: A stress run is only meaningful because predicate-based plan death now works: run on
#: pre-phase-0 code it measured a plan that could not die.
INVERTED_THESES = {
    "sec10-0811-no-entry": _thesis("UP", "overnight_high", 29887.0,
                                   falsify_at=29500.0),
    "sec10-0812": _thesis("UP", "overnight_high", 29992.25, falsify_at=29600.0),
    "sec10-0813": _thesis("DOWN", "oracle_dol", 29700.0, falsify_at=30300.0),
    "sec10-0814": _thesis("UP", "overnight_high", 30287.25, falsify_at=29900.0),
}

#: §10.2's conclusion, as a testable band. "the brakes hold every observed adverse day to
#: 0..-70 (deepest: 07-31 -67.50; 07-21 corrected to -42.25 with an attempt unspent)".
ADVERSE_BAND_PTS = (-70.0, 0.0)


def thesis_for(key: str) -> "dict | None":
    return ORACLE_THESES.get(key)


def inverted_thesis_for(key: str) -> "dict | None":
    return INVERTED_THESES.get(key)


# ---------------------------------------------------------------------------- #
# Plan 40 — unnested weekly/monthly extremes (`l2-target-selection.md` §7a)
# ---------------------------------------------------------------------------- #
#
# The flag is ON by operator decision (2026-09-22) but the Wave-4 adoption edits were
# skipped: there is no new ERA and no CASES entry; these are the measured figures that
# adoption would assert.
#
# The completed-period list as of the 2026-09-21 session open (2026-09-20 18:00 ET),
# computed from the 2026-12-era 1m parquet with the per-asset starts (MNQ 2026-06-16,
# MES 2026-06-11). name -> (price, aliases). Pinned by
# agent/facts/test_htf_extremes_real.py::test_0921_list_as_of_session_open.
#
# Three MNQ highs (30111.5, 30064.5, 29993.5) are unnested AT THE OPEN and are pruned
# by 09-21's own overnight bars before the first fill; at the 09:35:01 fill the nearest
# floor-passing UP row is the August month high 30639.50 (plan 40 insight 3). Its NAME
# is the month's, not the week's: Q3 dedupe keeps the month and records the week as an
# alias.
HTF_0921_AS_OF = "2026-09-20T18:00:00-04:00"
HTF_0921_MNQ = {
    "htf_month_high_202606": (31273.75, ("htf_week_high_20260616",)),
    "htf_week_high_20260622": (31267.5, ()),
    "htf_week_high_20260630": (30899.75, ()),
    "htf_month_high_202607": (30855.5, ()),
    "htf_month_high_202608": (30639.5, ("htf_week_high_20260817",)),
    "htf_week_high_20260828": (30111.5, ()),
    "htf_week_high_20260908": (30064.5, ()),
    "htf_week_high_20260918": (29993.5, ()),
    "htf_month_low_202607": (27499.75, ("htf_week_low_20260729",)),
    "htf_month_low_202608": (28612.75, ("htf_week_low_20260803",)),
    "htf_week_low_20260916": (29052.75, ()),
}
HTF_0921_MES = {
    "htf_month_high_202608": (7906.25, ("htf_week_high_20260813",)),
    "htf_week_high_20260817": (7892.5, ()),
    "htf_week_high_20260828": (7850.25, ()),
    "htf_week_high_20260903": (7834.0, ()),
    "htf_week_high_20260907": (7796.25, ()),
    "htf_week_high_20260918": (7739.0, ()),
    "htf_month_low_202606": (7362.75, ("htf_week_low_20260611",)),
    "htf_month_low_202607": (7391.0, ("htf_week_low_20260729",)),
    "htf_week_low_20260916": (7575.25, ()),
}

#: The 2026-09-21 oracle UP thesis, exactly as the live-worktree run
#: regression/sessions/2026-09-21/22-17-55 injected it. `exhausted_if` is NOT added,
#: although plan 40 asked for it: `fixed_backend.validate_oracle_thesis` rejects the
#: field (removed 2026-08-29 -- reaching the DOL is the exhaustion).
ORACLE_0921_UP = {
    "bias": "UP", "regime": "HYBRID", "confidence": "LOW",
    "dol": {"level": "week_high", "price": 30277.25},
    "falsified_if": [{"type": "price_beyond", "price": 29903.5, "side": "below"}],
}

#: Its replay, per arm (13:00 window, oracle, gate_arrival). Fills 09:35:01 @30251.50
#: (stopped 09:35:38 @30238.50, -13.00) and 09:37:00 @30263.75, both arms.
#:   flag OFF: T2 projection_up 30338.25, reached 09:44:59 -> +74.50; day +61.50.
#:   flag ON : T2 htf_month_high_202608 30639.50 (FAR), reached 11:50:49 -> +375.75;
#:             day +362.75. Initial synthetic_85pct 30581.30 / 30583.1375 (record only).
HTF_0921_UP_FILLS = (("2026-09-21T09:35:01-04:00", 30251.5),
                     ("2026-09-21T09:37:00-04:00", 30263.75))
HTF_0921_UP_T2 = ("htf_month_high_202608", 30639.5)
HTF_0921_UP_INITIAL = (30581.3, 30583.1375)
HTF_0921_UP_DAY_PTS = {"off": 61.5, "on": 362.75}


# --------------------------------------------------------------------------------------- #
# Plan 46 (§11.5 CANDIDATE) — the pre-move classifier on the operator-labelled days.
#
# Plain data, like `HTF_0921_MNQ`: what `premove_context.classify` returns at 09:20 on the
# study's back-adjusted `main/2026-12` MNQ 1m file, pinned by
# `test_premove_context_real.py::test_reproduces_the_study_on_the_operator_labelled_days`.
# `origin_ts` is ET. Every row reproduces `<global>/studies/premove_mid_retrace/context.csv`
# (COUNTER folds into UNRELATED: no COUNTER branch is built, §11.5 Q2).
#   labelled          the operator's 16 big days that are big here, plus the four the
#                     operator calls big that are NOT big mechanically (§11.5 Q1)
#   labelled_counter  07-30: the study's COUNTER day, UNRELATED here by construction
#   excluded          report / half days (excluded BY HAND from the A/B), still classified
# --------------------------------------------------------------------------------------- #
PREMOVE_CONTEXT_LABELLED = {
    "2025-10-17": {"status": "UNRELATED", "direction": "UP", "size": 392.25,
                   "origin_ts": "2025-10-17 04:20", "tag": "labelled"},
    "2025-11-17": {"status": "UNRELATED", "direction": "DOWN", "size": 351.75,
                   "origin_ts": "2025-11-17 03:57", "tag": "labelled"},
    "2026-03-13": {"status": "UNRELATED", "direction": "UP", "size": 337.75,
                   "origin_ts": "2026-03-13 04:11", "tag": "labelled"},
    "2026-03-26": {"status": "UNRELATED", "direction": "DOWN", "size": 348.25,
                   "origin_ts": "2026-03-25 23:40", "tag": "labelled"},
    "2026-04-17": {"status": "UNRELATED", "direction": "UP", "size": 369.25,
                   "origin_ts": "2026-04-17 01:03", "tag": "labelled"},
    "2026-04-30": {"status": "UNRELATED", "direction": "UP", "size": 363.0,
                   "origin_ts": "2026-04-30 02:11", "tag": "labelled"},
    "2026-05-08": {"status": "UNRELATED", "direction": "UP", "size": 434.75,
                   "origin_ts": "2026-05-07 18:00", "tag": "labelled"},
    "2026-06-08": {"status": "UNRELATED", "direction": "UP", "size": 443.5,
                   "origin_ts": "2026-06-08 05:10", "tag": "labelled"},
    "2026-07-21": {"status": "UNRELATED", "direction": "UP", "size": 497.5,
                   "origin_ts": "2026-07-20 20:07", "tag": "labelled"},
    "2026-07-23": {"status": "UNRELATED", "direction": "DOWN", "size": 435.0,
                   "origin_ts": "2026-07-23 05:03", "tag": "labelled"},
    "2026-08-25": {"status": "UNRELATED", "direction": "UP", "size": 403.25,
                   "origin_ts": "2026-08-24 20:19", "tag": "labelled"},
    "2026-09-01": {"status": "UNRELATED", "direction": "DOWN", "size": 488.25,
                   "origin_ts": "2026-09-01 01:57", "tag": "labelled"},
    "2026-07-30": {"status": "UNRELATED", "direction": "UP", "size": 539.75,
                   "origin_ts": "2026-07-30 04:02", "tag": "labelled_counter"},
    "2026-01-15": {"status": "PART", "direction": "UP", "size": 358.5,
                   "origin_ts": "2026-01-14 20:33", "tag": "labelled"},
    "2026-08-18": {"status": "PART", "direction": "DOWN", "size": 443.5,
                   "origin_ts": "2026-08-17 20:51", "tag": "labelled"},
    "2026-09-17": {"status": "PART", "direction": "UP", "size": 505.75,
                   "origin_ts": "2026-09-16 18:04", "tag": "labelled"},
    "2025-11-14": {"status": "NOT_BIG", "direction": "UP", "size": 105.75,
                   "origin_ts": "2025-11-14 08:44", "tag": "labelled"},
    "2026-02-06": {"status": "NOT_BIG", "direction": "DOWN", "size": 115.75,
                   "origin_ts": "2026-02-06 06:58", "tag": "labelled"},
    "2026-03-16": {"status": "NOT_BIG", "direction": "UP", "size": 211.75,
                   "origin_ts": "2026-03-16 05:45", "tag": "labelled"},
    "2026-03-27": {"status": "NOT_BIG", "direction": "DOWN", "size": 238.25,
                   "origin_ts": "2026-03-27 05:32", "tag": "labelled"},
    "2026-08-12": {"status": "NOT_BIG", "direction": "UP", "size": 142.0,
                   "origin_ts": "2026-08-12 08:31", "tag": "excluded"},
    "2026-03-06": {"status": "NOT_BIG", "direction": "DOWN", "size": 233.5,
                   "origin_ts": "2026-03-06 08:35", "tag": "excluded"},
    "2026-09-10": {"status": "UNRELATED", "direction": "DOWN", "size": 465.75,
                   "origin_ts": "2026-09-10 01:46", "tag": "excluded"},
    "2026-07-03": {"status": "PART", "direction": "UP", "size": 400.0,
                   "origin_ts": "2026-07-02 20:58", "tag": "excluded"},
}
