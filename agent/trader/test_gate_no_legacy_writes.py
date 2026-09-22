"""Gate 4 — the legacy engine is undisturbed.

The plan specifies this gate as a `grep`, which cannot tell CODE from a DOCSTRING that
merely names the forbidden thing (every module here documents what it must not touch,
so the grep reports its own warning labels as violations). This parses the modules
instead and inspects the AST: imports, call targets, attribute chains, and string
literals. That is the property the grep was reaching for.
"""
from __future__ import annotations

import ast
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
PACKAGES = (os.path.join(_AGENT, "facts"), os.path.join(_AGENT, "trader"),
            os.path.join(_AGENT, "study"))

FORBIDDEN_CALLS = frozenset({
    "save_daily", "save_position", "save_hypothesis", "save_smts", "save_global",
    "freeze_active_mgmt", "set_state_dir",
})
FORBIDDEN_FILENAMES = frozenset({
    "events.jsonl", "daily.json", "hypothesis.json", "position.json", "smts.json",
})
FORBIDDEN_MODULES = frozenset({"live_orders"})


def _modules():
    for pkg in PACKAGES:
        for root, _dirs, files in os.walk(pkg):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith(".py") and not name.startswith("test_"):
                    yield os.path.join(root, name)


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _docstring_nodes(tree):
    """Every node that IS a docstring, so prose is excluded from the string scan."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


@pytest.mark.parametrize("path", sorted(_modules()))
def test_module_never_imports_live_orders(path):
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_MODULES, path
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in FORBIDDEN_MODULES, path


@pytest.mark.parametrize("path", sorted(_modules()))
def test_module_never_calls_a_legacy_state_mutator(path):
    tree = _tree(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else None)
        assert name not in FORBIDDEN_CALLS, f"{path} calls {name}()"


@pytest.mark.parametrize("path", sorted(_modules()))
def test_module_never_names_a_legacy_state_file_in_code(path):
    """A docstring may WARN about `events.jsonl`; code may not mention it."""
    tree = _tree(path)
    docs = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docs:
                continue
            assert node.value not in FORBIDDEN_FILENAMES, (
                f"{path}:{getattr(node, 'lineno', '?')} references {node.value!r}")


def test_the_scan_actually_covers_the_new_packages():
    mods = list(_modules())
    assert any(p.endswith(os.path.join("trader", "executor.py")) for p in mods)
    assert any(p.endswith(os.path.join("facts", "batch.py")) for p in mods)
    assert len(mods) >= 15
    # Plan 40: the HTF-extremes modules, including the one that writes htf_extremes.json.
    assert any(p.endswith(os.path.join("facts", "htf_extremes.py")) for p in mods)
    assert any(p.endswith(os.path.join("facts", "htf_source.py")) for p in mods)


def test_the_htf_artifact_is_not_a_legacy_state_file():
    """Plan 40's run-folder artifact is scanned by the gates above like every other
    literal under agent/facts; this pins that its NAME is not a legacy file's."""
    from agent.facts.htf_source import ARTIFACT
    assert ARTIFACT == "htf_extremes.json"
    assert ARTIFACT not in FORBIDDEN_FILENAMES


def test_session_pipeline_trader_hook_is_additive_not_a_bypass():
    """The hook must not early-return: the legacy engine below it has to keep running."""
    import inspect
    import session_pipeline
    src = inspect.getsource(session_pipeline.SessionPipeline.on_1m_bar)
    idx = src.index("self._trader is not None")
    assert "return []" not in src[idx: idx + 400]
    # ...and the legacy daily block still follows it.
    assert src.index("_last_daily_minute") > idx
