"""Integration tests for the post-hoc sweep driver (GIL-44 Phase 1).

Exercises the freeform-report parser + validator end-to-end against the real POC
run reports:
  - runs 3 and 5 are clean  -> zero violations (no false positives)
  - run 4 seeded the fractional-vote violation (D1/D3 votes = +0.5) -> caught
and asserts the <1s-per-report budget.
"""

import os
import time

from validate_results import parse_facts, parse_report, validate_report

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RUN5 = os.path.join(HERE, "fixtures", "poc-result-run5.md")
POC_FACTS = os.path.join(HERE, "fixtures", "facts-run5.txt")
RUN3 = os.path.join(HERE, "fixtures", "poc-result-run3.md")
RUN4 = os.path.join(HERE, "fixtures", "poc-result-run4.md")


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #
def test_parse_run5_extracts_directions_and_confidence():
    d = parse_report(open(RUN5, encoding="utf-8").read())
    assert d["daily_trend"]["direction"] == "down"
    assert d["daily_trend"]["confidence"] == "high"
    assert d["next_move"]["direction"] == "down"
    assert d["next_move"]["confidence"] == "medium"


def test_parse_run5_extracts_driver_votes():
    d = parse_report(open(RUN5, encoding="utf-8").read())
    votes = {drv["id"]: drv["vote"] for drv in d["daily_trend"]["drivers"]}
    # run 5 driver table: D1 -1, D2 -1, D3 0, D4 -1, D5 0, D6 +1
    assert votes == {"D1": -1, "D2": -1, "D3": 0, "D4": -1, "D5": 0, "D6": 1}


def test_parse_run4_extracts_fractional_votes():
    d = parse_report(open(RUN4, encoding="utf-8").read())
    votes = {drv["id"]: drv["vote"] for drv in d["daily_trend"]["drivers"]}
    assert votes["D1"] == 0.5
    assert votes["D3"] == 0.5


def test_parse_facts_extracts_price_and_checkpoint():
    facts = parse_facts(open(POC_FACTS, encoding="utf-8").read())
    assert isinstance(facts["now_price"], float)
    assert facts["checkpoint"]  # non-empty timestamp string


# --------------------------------------------------------------------------- #
# End-to-end validation                                                        #
# --------------------------------------------------------------------------- #
def test_run5_is_clean():
    result = validate_report(RUN5)
    assert result.ok, f"run 5 falsely flagged: {result.messages()}"


def test_run3_is_clean():
    result = validate_report(RUN3)
    assert result.ok, f"run 3 falsely flagged: {result.messages()}"


def test_run4_fractional_vote_caught():
    result = validate_report(RUN4)
    assert "ARI_FRACTIONAL_VOTE" in result.codes(), result.messages()


def test_parse_result_block_is_authoritative():
    """Calibration cuts end with the machine-readable RESULT block (poc_template).
    It must win over any prose in the body."""
    report = """# some report body
    ...prose that says direction: up somewhere...

RESULT:
daily_direction: down
daily_confidence: high
next_direction: neutral
next_confidence: low
move_target: none
long_resolution: 30100.0
short_resolution: 29900.0
"""
    d = parse_report(report)
    assert d["daily_trend"]["direction"] == "down"
    assert d["daily_trend"]["confidence"] == "high"
    assert d["next_move"]["direction"] == "neutral"
    assert d["next_move"]["confidence"] == "low"


def test_parse_extended_result_block_fields():
    """The v2 RESULT block carries regime/day_dol/weakens/flips/arm so the parser
    never depends on freeform heading styles for the required daily fields
    (run-6 false-positive regression)."""
    report = """# body

RESULT:
daily_direction: down
daily_confidence: medium
daily_regime: hybrid
day_dol: 29406.25
weakens_to_neutral_if: 2x5m closes above daily mid 29581.375
flips_if: 2x5m closes above prev1_day_high 29690.25
next_direction: neutral
next_confidence: low
arm_entry_confirmation: no
move_target: none
long_resolution: above 29581.375 -> 29756.5
short_resolution: below 29406.25 -> 29350.0
"""
    d = parse_report(report)
    daily = d["daily_trend"]
    assert daily["regime"] == "hybrid"
    assert daily["day_dol"] == "present"
    assert daily["weakens_to_neutral_if"] == "present"
    assert daily["flips_if"] == "present"
    assert d["next_move"]["arm_entry_confirmation"] == "no"


def test_parse_heading_style_fields_run6():
    """Freeform heading styles from the run-6 report parse without false
    SYN_MISSING_FIELD: '### Regime' prose, '### Day of Loss (DOL)',
    'Weakens to neutral if:' / 'Flips to UP if:' bullets, and a bare
    '### arm_entry_confirmation' heading with the value on the next line."""
    report = """## 1. DAILY-TREND DECISION

direction: down
confidence: medium

### Regime

Classification: **HYBRID** (range-leaning with bearish breakdown signals).

### Day of Loss (DOL)

Most recent confirmed lower pivot: london low 29498.25.

### Weakens/Flips

- **Weakens to neutral if:** 2 consecutive 5m closes above daily mid.
- **Flips to UP if:** 2 consecutive 5m closes above prev1_day_high.

## 2. NEXT-MOVE DECISION

direction: neutral
confidence: low

### arm_entry_confirmation

**no** - next-move is NEUTRAL.
"""
    d = parse_report(report)
    daily = d["daily_trend"]
    assert daily["regime"] == "hybrid"
    assert daily["day_dol"] == "present"
    assert daily["weakens_to_neutral_if"] == "present"
    assert daily["flips_if"] == "present"
    assert d["next_move"]["arm_entry_confirmation"] == "no"


def test_report_validates_under_one_second():
    start = time.perf_counter()
    validate_report(RUN5)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"validation took {elapsed:.3f}s (budget 1s)"
