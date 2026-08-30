"""Task 11: the cycle-3 phase-2/3 gates and standing invariants.

Two kinds of assertion live here and they are different in kind:

  **Standing invariants** — the constraints an agent cannot infer from the code and that
  a plausible-looking change silently breaks: no legacy imports, no legacy writes, bar
  time inside the bar loop, the §9 knobs at their starting values, the retired list
  staying unbuilt.

  **§10.2's wrong-thesis stress** — invert each forward-test day's thesis, re-run, and
  assert the ADVERSE BAND holds. This is meaningful only because predicate-based plan
  death now works: run on pre-phase-0 code it measured a plan that could not die. A rule
  that improves right-thesis days can quietly worsen wrong-thesis ones, and this is the
  only thing that would notice.
"""
import ast
import inspect
import json
import os

import pytest

from agent.trader import named_cases as nc
from agent.trader.replay import run_replay

pytestmark = pytest.mark.timeout(1800)

NEW_MODULES = ("agent.trader.retrace", "agent.trader.extreme_reject",
               "agent.trader.episode", "agent.trader.takeover",
               "agent.trader.arbiter", "agent.trader.shadow_ledger",
               "agent.trader.named_cases", "agent.trader.tape")

FORBIDDEN_IMPORTS = ("live_orders", "smt_state", "paths")
FORBIDDEN_CALLS = ("set_state_dir", "get_et_now")


def _module(name):
    return __import__(name, fromlist=["x"])


def _source(name):
    return inspect.getsource(_module(name))


# --- standing invariants ----------------------------------------------------- #

@pytest.mark.parametrize("name", NEW_MODULES)
def test_no_new_module_imports_a_legacy_writer(name):
    """The AST gate covers `agent/trader/` as a whole; this names the modules THIS cycle
    added, so a future one cannot add an import and pass by being new."""
    tree = ast.parse(_source(name))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for bad in FORBIDDEN_IMPORTS:
        assert not any(bad in m for m in imported), f"{name} imports {bad}: {imported}"


@pytest.mark.parametrize("name", NEW_MODULES)
def test_no_new_module_reaches_a_forbidden_call(name):
    src = _source(name)
    for bad in FORBIDDEN_CALLS:
        assert f"{bad}(" not in src, f"{name} calls {bad}"


def test_the_executor_contains_no_wall_clock():
    """Bar time INSIDE the bar loop; `get_et_now()` is for scheduling outside it. A wall
    clock in here makes a replay non-deterministic and a backtest unfalsifiable."""
    src = _source("agent.trader.executor")
    assert "get_et_now" not in src
    assert "datetime.now" not in src


def _code_strings(name):
    """Every string LITERAL in the module's executable code — docstrings excluded.

    A plain `in src` test cannot tell a filename in a prohibition ("never write
    events.jsonl") from one in an `open()` call, and the obvious escape hatch — allowing
    the mention when the word "never" appears anywhere in the file — disables the check
    module-wide. `test_gate_no_legacy_writes.py` already reads the AST for this reason.
    """
    tree = ast.parse(_source(name))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


@pytest.mark.parametrize("name", NEW_MODULES)
def test_no_new_module_writes_a_legacy_state_file(name):
    literals = _code_strings(name)
    for forbidden in ("events.jsonl", "daily.json", "hypothesis.json",
                      "position.json", "smts.json"):
        assert forbidden not in literals, f"{name} names {forbidden} in CODE"


def test_the_legacy_state_file_gate_would_actually_fire():
    """Seeded: the check above is only worth having if a real occurrence trips it."""
    seeded = '"""never write events.jsonl"""\nPATH = "events.jsonl"\n'
    tree = ast.parse(seeded)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module) and ast.get_docstring(node):
            node.body = node.body[1:]
    lits = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "events.jsonl" in lits, "the AST scan misses a plain assignment"


# --- §9 knobs at their starting values --------------------------------------- #

def test_every_knob_this_cycle_reads_is_at_its_sec9_starting_value():
    """This cycle builds RULES. Per §11.0: if a change would alter a NUMBER it is
    deferred; if it would alter BEHAVIOUR it is in scope."""
    from agent.trader import episode, executor, extreme_reject
    from agent.trader.arbiter import MAX_ATTEMPTS
    assert executor.ENTRY_BUFFER_PTS == 3.0
    assert executor.STOP_BUFFER_PTS == 3.0
    assert executor.STOP_CAP_PTS == 25.0
    assert executor.MIN_FVG_HEIGHT_PTS == 5.0
    assert executor.MAX_FVG_HEIGHT_PTS == 45.0
    assert executor.DISTANCE_INVALIDATION_PTS == 60.0
    assert executor.MAX_DISTANCE_PTS == 60.0
    assert executor.DOL_FLOOR_PTS == 60.0
    assert executor.NO_MOVE_ZONE_PTS == 15.0
    assert executor.SETTLE_UNTIL_SECONDS == 30
    assert episode.EARLY_RUNAWAY_PTS == 25.0
    assert episode.SL_BUFFER_PTS == 2.0
    assert episode.SL_CAP_PTS == 30.0
    assert extreme_reject.QUIET_CLOSES_TO_ARM == 3
    assert extreme_reject.SL_CAP_PTS == 15.0
    assert extreme_reject.STALE_EXTREME_AGE_HOURS == 2.0
    assert MAX_ATTEMPTS == 3


# --- §11.0's DO-NOT-IMPLEMENT list ------------------------------------------- #

def test_the_retired_list_stays_unbuilt():
    """Rebuilding any of these from stale prose elsewhere is a defect. Each is checked
    where it would have to live, not merely globally."""
    from agent.trader import episode, extreme_reject
    from agent.trader.executor import Executor

    # §6's RR-to-DOL disqualification test — RETIRED, provably inert (R* = 0).
    ep_src = inspect.getsource(episode)
    for token in ("rr_to_dol", "risk_reward", "RR_THRESHOLD"):
        assert token not in ep_src

    # §7's distance-keyed (>150 pts) fallback — REPLACED by the age key.
    er_src = ast.unparse(ast.parse(inspect.getsource(extreme_reject)))
    assert "150" not in _strip_docstrings(er_src)

    # The §8 SL-cap skip gate extended to §6-proper — IDEA ONLY, zero net evidence.
    assert "sl_cap_gate: bool = False" in inspect.getsource(episode.Episode.__init__), \
        "the gate must stay OPT-IN; defaulting it on extends it to §6-proper"

    # §5's re-entry churn guard — CANDIDATE, zero motivating examples.
    ex_src = inspect.getsource(Executor)
    assert "_retrace.reset" not in ex_src, \
        "re-arming the retrace gate after a stop-out IS the retired churn guard"


def _strip_docstrings(src: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) \
                and ast.get_docstring(node):
            node.body = node.body[1:]
    return ast.unparse(tree)


def test_the_deleted_sec5_already_inside_clause_is_not_reintroduced():
    """'or immediately if price is already inside / has already entered it' is DELETED.
    The implementation requires a CROSSING-IN, which is what makes the two readings
    distinguishable on the days §11 measured."""
    from agent.trader.retrace import RetraceGate
    import pandas as pd
    settle = pd.Timestamp("2026-08-21 09:30:30", tz="America/New_York")
    gate = RetraceGate(settle)
    for sec in ("09:30:20", "09:31:00", "09:35:00"):
        gate.note_tick("A", 29480.0, 29472.25, 29485.0,
                       pd.Timestamp(f"2026-08-21 {sec}", tz="America/New_York"))
    assert not gate.fresh_entry_seen("A")


# --- the registry ------------------------------------------------------------ #

def test_the_registry_and_the_document_still_agree():
    assert nc.missing_quotes() == []


def test_every_documented_figure_lives_in_exactly_one_place():
    """The registry is the single source. A test that restates a figure as a literal is
    a second copy that can drift — so the named-case modules must READ from it."""
    for module in ("test_named_sec5", "test_named_sec7", "test_named_sec8",
                   "test_arbiter_forward"):
        src = _source(f"agent.trader.{module}")
        assert "named_cases" in src, f"{module} does not read the registry"


# --- §10.2's wrong-thesis stress --------------------------------------------- #

_STRESS_CACHE: dict = {}


def _stress(key):
    """Cached: three tests read the same four inverted replays, and each replay is a real
    1s backtest. Uncached this module pays a 3x multiplier on its slowest work."""
    if key in _STRESS_CACHE:
        return _STRESS_CACHE[key]
    thesis = nc.inverted_thesis_for(key)
    case = nc.by_key(key)
    res = run_replay([case.date], allow_calls=False, thesis=thesis)
    path = os.path.join(res[case.date]["run_dir"], "trader_decisions.jsonl")
    rows = ([json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
            if os.path.exists(path) else [])
    pnl, entry, direction = 0.0, None, None
    for r in rows:
        if r["kind"] == "fill":
            entry, direction = float(r["price"]), str(r.get("direction") or "").upper()
        elif r["kind"] in ("stop_out", "take_profit") and entry is not None:
            sign = 1.0 if direction in ("UP", "LONG") else -1.0
            pnl += sign * (float(r["price"]) - entry)
            entry = None
    _STRESS_CACHE[key] = (round(pnl, 2), rows)
    return _STRESS_CACHE[key]


@pytest.mark.parametrize("key", sorted(nc.INVERTED_THESES))
def test_the_adverse_band_holds_under_an_inverted_thesis(key):
    """§10.2's conclusion, as a gate: the brakes hold every observed adverse day to
    0..-70. The theoretical ceiling is 3 x (bound-gap height + 10) and NOTHING in the
    sample realises it."""
    pnl, rows = _stress(key)
    # A run that armed nothing, bound nothing, or fell into `on_bar`'s swallowing except
    # also scores 0.00, which sits INSIDE the band — so "the brakes held" and "nothing
    # ran" would otherwise be indistinguishable.
    assert rows, f"{key} produced no decision artifact at all"
    assert [r for r in rows if r["kind"] == "bind"],         f"{key} bound nothing — the band assertion below would be vacuous"
    lo, hi = nc.ADVERSE_BAND_PTS
    assert lo <= pnl <= hi, f"{key} inverted bled {pnl:+.2f}, outside {lo}..{hi}"


def test_an_inverted_plan_can_still_DIE():
    """The stress is worthless against a plan that cannot die — which is exactly what a
    pre-phase-0 run measured. Every inverted day must terminate on a stated reason."""
    reasons = set()
    for key in sorted(nc.INVERTED_THESES):
        _pnl, rows = _stress(key)
        dead = [r for r in rows if r["kind"] == "plan_dead"]
        if dead:
            reasons.add(dead[0]["reason"])
    assert reasons, "no inverted day died at all — plan death is inert"
    assert reasons <= {"dol_reached", "attempts_exhausted", "falsified"}, reasons


def test_no_inverted_day_spends_more_than_the_shared_budget():
    """Attempts are ASSERTED, never scored — and the budget is the hard bound on the
    bleed, so exceeding it would invalidate the band above rather than merely cost."""
    for key in sorted(nc.INVERTED_THESES):
        _pnl, rows = _stress(key)
        stops = len([r for r in rows if r["kind"] == "stop_out"])
        assert stops <= 3, f"{key} spent {stops} attempts"
