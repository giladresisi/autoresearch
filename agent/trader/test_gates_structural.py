"""Structural gates that used to live inside ordinary unit-test modules.

These assertions were scattered through `test_analyzer.py`, `test_graft.py`,
`test_executor.py`, `test_shadow_ledger.py`, `test_records.py` and
`test_comparison.py` — files whose OTHER tests cover churning internals. Deleting
those files by name would have silently removed the safety net with them, so the
gates were lifted out here first.

Each one is either stricter than, or outside the reach of, the two package-wide
gates that already exist:

  * `test_gate_no_legacy_writes.py` parses the AST of every module under
    `agent/{facts,trader,study}` — it catches import statements, call targets and
    string LITERALS. It does not see a name that appears only in a docstring.
  * `test_gate_mechanisms.py::test_the_executor_contains_no_wall_clock` covers the
    Executor's determinism, and its FORBIDDEN_IMPORTS sweep covers the cycle-3
    NEW_MODULES list only.
  * `test_executor.py` keeps its own raw-source `live_orders` grep, where CLAUDE.md
    documents it. That file is not slated for deletion, so the gate stays put.

The raw-source bans below are deliberately coarser than an AST walk: they grep the
whole module text, docstrings included. CLAUDE.md mandates that specifically for the
Executor, and the same reasoning applies to the Analyzer and the graft — a module
that merely NAMES the legacy order path is already too close to it.

Six behavioural siblings ("...writes no legacy state file": one each in
test_analyzer, test_graft, test_executor, test_shadow_ledger, test_planner) were
NOT lifted. Each instantiated its subject with a full fixture stack and asserted no
forbidden file appeared in a tmp_path, which is strictly weaker than
`test_gate_no_legacy_writes.py::test_module_never_names_a_legacy_state_file_in_code`
for any literal filename, and would have dragged the churning fixtures in here.
The residual gap is a filename built dynamically. That is NOT hypothetical:
`scripts/compare_analyzer_vs_legacy.py` assembles `"events" + ".jsonl"` with a comment
saying it splits the string so the module never contains the literal. It is benign
there (that module is read-only, and `scripts/` is outside the AST gate's package
list), but it shows the static scan can be walked past. Closing that gap means
extending the scan, not restoring the behavioural duplicates.
"""
from __future__ import annotations

import ast
import importlib
import inspect

import pytest

# Module -> tokens that must not appear ANYWHERE in its source, docstrings included.
RAW_SOURCE_BANS = {
    # A wall clock inside the bar loop makes a replay non-deterministic and a
    # backtest unfalsifiable. The Executor's copy of this lives in
    # test_gate_mechanisms.py; this is the Analyzer's.
    "agent.trader.analyzer": ("live_orders", "get_et_now", "datetime.now",
                              "Timestamp.now"),
    "agent.trader.graft": ("live_orders",),
}

# The ledger is a LOGGING HOOK ONLY: it must not be able to reach a trading path.
LEDGER_UNREACHABLE = ("order_sim", "live_orders", "executor")


@pytest.mark.parametrize("module,banned", sorted(RAW_SOURCE_BANS.items()))
def test_module_source_never_names_a_forbidden_token(module, banned):
    src = inspect.getsource(importlib.import_module(module))
    for token in banned:
        assert token not in src, f"{module} names {token!r}"


def test_the_shadow_ledger_imports_nothing_that_can_trade():
    mod = importlib.import_module("agent.trader.shadow_ledger")
    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(bad in m for m in imported for bad in LEDGER_UNREACHABLE), imported


def test_decisions_file_is_not_events_jsonl():
    from agent.trader.records import DECISIONS_FILE
    assert DECISIONS_FILE != "events.jsonl" and "events" not in DECISIONS_FILE


# `open(path, "r+")` writes; `"rb"` does not. Mode chars that imply a write:
_WRITE_MODE_CHARS = frozenset("wax+")

# Methods that put bytes on disk. `.write`/`.writelines` are included and the only
# permitted receivers are the std streams, which this CLI renders its table to.
_WRITING_METHODS = frozenset({
    "write", "writelines", "write_text", "write_bytes", "truncate",
    "mkdir", "makedirs", "touch", "unlink", "remove", "rmdir", "rmtree",
    "rename", "replace", "dump", "to_csv", "to_json", "to_parquet",
})
_ALLOWED_WRITE_RECEIVERS = frozenset({"stdout", "stderr"})


def _open_mode(call):
    """The mode string of an `open()`-family call, or None if absent/not literal."""
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    return None


def _receiver_name(node):
    """`sys.stdout.write` -> 'stdout'; `fh.write` -> 'fh'."""
    recv = node.func.value
    if isinstance(recv, ast.Attribute):
        return recv.attr
    return recv.id if isinstance(recv, ast.Name) else None


def test_the_comparison_script_never_writes_to_disk():
    """It reads the LEGACY `events.jsonl`. A stray write there would invalidate every
    locked comparison, so the module must hold no filesystem write path at all."""
    import scripts.compare_analyzer_vs_legacy as mod
    tree = ast.parse(inspect.getsource(mod))
    offences = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "open":
            mode = _open_mode(node)
            if mode and _WRITE_MODE_CHARS & set(mode):
                offences.append(f"line {node.lineno}: open(..., {mode!r})")
        elif isinstance(fn, ast.Attribute) and fn.attr in _WRITING_METHODS:
            if fn.attr == "open":
                continue
            if _receiver_name(node) in _ALLOWED_WRITE_RECEIVERS:
                continue
            offences.append(f"line {node.lineno}: .{fn.attr}()")
    assert not offences, "comparison script can write to disk: " + "; ".join(offences)


def test_the_comparison_script_still_reads_the_legacy_stream():
    """Guards the gate above from passing vacuously if the reader is ever removed.

    The module assembles the name as `"events" + ".jsonl"` so its own source carries no
    literal for a filename scanner to trip on, so assert the VALUE, not the spelling.
    """
    import scripts.compare_analyzer_vs_legacy as mod
    assert mod.LEGACY_LOG == "events.jsonl"
    assert "open(" in inspect.getsource(mod)


def test_this_file_still_guards_every_module_it_claims_to():
    """A rename upstream must not turn a gate into a no-op that still passes."""
    for module in RAW_SOURCE_BANS:
        assert importlib.import_module(module) is not None
