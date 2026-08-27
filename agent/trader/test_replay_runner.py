# agent/trader/test_replay_runner.py
# Part 1 (Task 6): the run_backtest_v2 pass-through.
# Part 2 (Task 7): the replay module and CLI.
import inspect

import pandas as pd
import pytest

TZ = "America/New_York"


# -- Task 6 ------------------------------------------------------------------ #

def test_run_backtest_v2_accepts_a_trader_factory_and_a_replay_window():
    import backtest_smt
    sig = inspect.signature(backtest_smt.run_backtest_v2)
    assert "trader_factory" in sig.parameters
    assert "replay_window" in sig.parameters
    assert sig.parameters["trader_factory"].default is None
    assert sig.parameters["replay_window"].default is None


def test_the_pipeline_is_constructed_with_the_trader_argument():
    """The seam already exists in SessionPipeline; this asserts the backtest uses it."""
    import backtest_smt
    src = inspect.getsource(backtest_smt.run_backtest_v2)
    assert "trader=" in src, "must pass the graft through the existing constructor seam"


def test_the_replay_window_is_applied_through_replay_slice():
    import backtest_smt
    src = inspect.getsource(backtest_smt.run_backtest_v2)
    assert "replay_slice(" in src


def test_defaults_leave_the_legacy_path_byte_identical():
    """Both new parameters default to None; with them unset the function must not
    reference the trader or the window at all on the executed path."""
    import backtest_smt
    src = inspect.getsource(backtest_smt.run_backtest_v2)
    assert "if trader_factory is not None" in src or "trader_factory and" in src
    assert "if replay_window" in src


# -- Task 7 ------------------------------------------------------------------ #

def test_a_replay_window_outside_1s_mode_is_refused_not_ignored():
    """CODE-REVIEW FINDING. Only the 1s branch applies the window; a 1m caller silently
    got a FULL session while believing it had asked for 09:20-11:00."""
    import backtest_smt
    with pytest.raises(ValueError):
        backtest_smt.run_backtest_v2("2026-08-12", "2026-08-12", mode="1m",
                                     replay_window=(pd.Timestamp("2026-08-12 09:20",
                                                                 tz=TZ),
                                                    pd.Timestamp("2026-08-12 11:00",
                                                                 tz=TZ)))


def test_the_window_is_0920_to_1100_et():
    from agent.trader.replay import replay_window_for
    w0, w1 = replay_window_for("2026-08-12")
    assert (w0.hour, w0.minute) == (9, 20)
    assert (w1.hour, w1.minute) == (11, 0)
    assert w0.tzinfo is not None and str(w0.tz) == TZ


def test_the_window_end_is_1100_not_the_dol():
    """The run must ALWAYS reach 11:00. Stopping at the DOL makes two A/B variants cover
    different windows whenever a change moves the touch time."""
    from agent.trader.replay import replay_window_for
    _, w1 = replay_window_for("2026-08-12")
    assert w1 == pd.Timestamp("2026-08-12 11:00", tz=TZ)


def test_the_default_arrival_latency_is_the_measured_p50():
    from agent.trader.replay import DEFAULT_ARRIVAL_LATENCY_SEC
    assert DEFAULT_ARRIVAL_LATENCY_SEC == 40.0


def test_the_replay_analyzer_runs_inline_not_threaded(tmp_path):
    """Threading would make the thesis land on an arbitrary bar depending on machine
    speed, destroying replay-vs-replay determinism."""
    from agent.trader.replay import build_replay_trader
    g = build_replay_trader("2026-08-12", tmp_path, allow_calls=False,
                            arrival_latency_sec=40.0)
    assert g._analyzer._threaded is False


def test_the_replay_analyzer_carries_the_arrival_gate(tmp_path):
    from agent.trader.replay import build_replay_trader
    g = build_replay_trader("2026-08-12", tmp_path, allow_calls=False,
                            arrival_latency_sec=40.0)
    assert g._analyzer._arrival == 40.0


def test_the_replay_backend_is_the_cached_one(tmp_path):
    from agent.trader.cached_backend import CachedThesisBackend
    from agent.trader.replay import build_replay_trader
    g = build_replay_trader("2026-08-12", tmp_path, allow_calls=False,
                            arrival_latency_sec=40.0)
    assert isinstance(g._analyzer._backend, CachedThesisBackend)


def test_allow_calls_false_is_the_default_for_run_replay():
    """A replay must not spend money or lose determinism by accident."""
    import inspect
    from agent.trader.replay import run_replay
    assert inspect.signature(run_replay).parameters["allow_calls"].default is False


def _fake_backtest(seen, run_dir):
    """Stands in for `run_backtest_v2`, INCLUDING its call to the trader factory.

    A fake that skipped the factory would leave `run_dir` unset, which `run_replay` now
    treats as "this date was skipped, not replayed" and reports rather than swallowing.
    """
    def _fake(start, end, **kw):
        seen.update(kw)
        seen["start"] = start
        factory = kw.get("trader_factory")
        if factory is not None:
            seen["graft"] = factory(start, run_dir)
        return {}
    return _fake


def test_run_replay_forwards_the_window_and_factory_to_the_backtest(monkeypatch,
                                                                    tmp_path):
    from agent.trader import replay as R
    seen = {}
    monkeypatch.setattr(R, "run_backtest_v2", _fake_backtest(seen, tmp_path))
    R.run_replay(["2026-08-12"])
    assert seen["mode"] == "1s"
    assert seen["trader_factory"] is not None
    assert seen["replay_window"][0] == pd.Timestamp("2026-08-12 09:20", tz=TZ)


def test_run_replay_reports_a_date_it_could_not_replay(monkeypatch):
    """CODE-REVIEW FINDING. A date with no bars in the window never reaches the factory,
    so the run completed 'successfully' with run_dir=None -- the silent-empty result
    shape this cycle exists to stop repeating."""
    from agent.trader import replay as R
    monkeypatch.setattr(R, "run_backtest_v2", lambda *a, **kw: {})
    with pytest.raises(RuntimeError, match="skipped, not replayed"):
        R.run_replay(["2026-08-12"])


def test_run_replay_never_writes_a_regression_baseline():
    """regression.py's locked recordings are the legacy engine's protection.

    The plan asserted `"baseline" not in inspect.getsource(R).lower()`. That is a grep
    over prose as well as code -- the same defect `test_gate_no_legacy_writes.py`'s
    docstring calls out ("cannot tell CODE from a DOCSTRING that merely names the
    forbidden thing"), and it fired the moment the module documented what it must not
    write. Parse instead, and exclude docstrings, exactly as that gate does.
    """
    import ast
    import agent.trader.replay as R

    tree = ast.parse(open(R.__file__, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None) or []
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            assert "baseline" not in node.value.lower(), \
                f"replay.py references a baseline path: {node.value!r}"
        if isinstance(node, ast.Attribute):
            assert "baseline" not in node.attr.lower()
        if isinstance(node, ast.Name):
            assert "baseline" not in node.id.lower()


def test_run_replay_writes_nothing_into_a_baseline_directory(monkeypatch, tmp_path):
    """The behavioural half: whatever the source says, no write lands under a
    `baseline` path."""
    import builtins
    from agent.trader import replay as R

    opened = []
    real_open = builtins.open

    def _spy(path, mode="r", *a, **kw):
        if any(w in str(mode) for w in ("w", "a", "x")):
            opened.append(str(path))
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", _spy)
    monkeypatch.setattr(R, "run_backtest_v2", _fake_backtest({}, tmp_path))
    R.run_replay(["2026-08-12"])
    assert not [p for p in opened if "baseline" in p.lower()]


def test_the_cli_exposes_dates_seed_and_latency_flags():
    import os
    import subprocess
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, os.path.join(repo, "scripts",
                                                       "replay_session.py"), "--help"],
                         capture_output=True, text=True, cwd=repo)
    assert "--dates" in out.stdout
    assert "--seed" in out.stdout
    assert "--arrival-latency-sec" in out.stdout
    assert "--clear-cache" in out.stdout
