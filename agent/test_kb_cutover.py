import importlib.util
import os
import sys
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _manual_kb():
    path = os.path.join(REPO, "manual-l1-thesis", "test_l1_thesis_manual.py")
    src = open(path, encoding="utf-8").read()
    ns = {}
    block = src[src.index("MANUAL_KB_FILES = ("): src.index(")", src.index("MANUAL_KB_FILES = (")) + 1]
    exec(block, ns)
    return ns["MANUAL_KB_FILES"]


def test_production_kb_equals_the_validated_manual_kb():
    """Every recorded L1 run used MANUAL_KB_FILES. Production must match it exactly."""
    from agent.run_agent import KB_FILES
    assert tuple(KB_FILES) == tuple(_manual_kb())


def test_thesis_md_is_wired_in():
    from agent.run_agent import KB_FILES
    assert "decisions/thesis.md" in KB_FILES


def test_v1_decision_docs_are_removed_not_merely_supplemented():
    """thesis.md REPLACES daily-trend + next-move; keeping both = contradictory policy."""
    from agent.run_agent import KB_FILES
    assert "decisions/daily-trend.md" not in KB_FILES
    assert "decisions/next-move.md" not in KB_FILES


def test_system_prompt_is_byte_stable_across_calls():
    from agent.run_agent import build_system_prompt
    assert build_system_prompt() == build_system_prompt()


def test_every_kb_file_exists_on_disk():
    from agent.run_agent import KB_FILES, DOCS_ROOT
    for rel in KB_FILES:
        assert os.path.exists(os.path.join(DOCS_ROOT, rel.replace("/", os.sep))), rel


# --- added during implementation (not in the plan) --------------------------- #

def test_system_prompt_matches_the_manual_harness_byte_for_byte():
    """The cutover's whole point: production and the recorded-run harness now build
    the SAME system prompt. Anything less invalidates every recorded L1 result."""
    from agent.run_agent import build_system_prompt, DOCS_ROOT
    sep = "\n\n---\n\n"
    blocks = []
    for rel in _manual_kb():
        path = os.path.join(DOCS_ROOT, rel.replace("/", os.sep))
        with open(path, encoding="utf-8", newline="") as fh:
            blocks.append(f"# FILE: {rel}\n\n{fh.read()}")
    assert build_system_prompt() == sep.join(blocks)


def test_thesis_md_no_longer_claims_it_is_unwired():
    """The stale authoring banner would tell the model its own policy is inert."""
    from agent.run_agent import DOCS_ROOT
    path = os.path.join(DOCS_ROOT, "decisions", "thesis.md")
    head = open(path, encoding="utf-8").read()[:2000]
    assert "NOT wired into `run_agent.KB_FILES`" not in head
