# tests/test_terminate_clean_shutdown.py
# O4: an in-session `trade.py terminate` stops the orchestrator through its NORMAL exit path
# instead of timing out into a force-kill. Covers the stop-file poll in ProcessManager, the
# orchestrator's stop branch, automation.main's stop-request watcher, and the trade.py side
# (shared stop-file path, position refusal, stale pid file, pause state, restart note).
#
# Nothing here starts a real orchestrator, IB connection or order: processes are mocks, the
# stop files are redirected by conftest's _isolate_stop_requests, and ACT_GLOBAL_DIR by
# _isolate_global_state.

from __future__ import annotations

import datetime
import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import psutil
import pytest

from orchestrator import stop_request
from orchestrator.output import OutputChannel
from orchestrator.process import ProcessManager
from orchestrator.relay import SessionRelay

_ET = ZoneInfo("America/New_York")
_FAR_FUTURE = datetime.datetime(2999, 1, 1, tzinfo=_ET)
_AUTOMATION_CMD = [sys.executable, "-m", "automation.main"]


def _pm(script=None):
    log = OutputChannel()
    lines = []
    sink = MagicMock()
    sink.write.side_effect = lambda t: lines.append(t)
    log.add_sink(sink)
    return ProcessManager(script or _AUTOMATION_CMD, SessionRelay(OutputChannel()), log), lines


def _running_proc(polls_before_exit=None, returncode=1, on_poll=None):
    """A mock child that is alive (poll() -> None) until `polls_before_exit` polls have
    happened; None = never exits by itself. No mock here gets an integer pid: _terminate
    looks up the pid's descendants, and a real-looking one could name a real process."""
    proc = MagicMock()
    proc.stdout = iter([])
    proc.returncode = returncode
    calls = {"n": 0}

    def _poll():
        calls["n"] += 1
        if on_poll is not None:
            on_poll(calls["n"])
        if polls_before_exit is not None and calls["n"] > polls_before_exit:
            return returncode
        return None

    proc.poll.side_effect = _poll
    proc.poll_calls = calls
    return proc


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr("orchestrator.process._POLL_INTERVAL_S", 0.001)


# ---------------------------------------------------------------------------
# ProcessManager._monitor
# ---------------------------------------------------------------------------

def test_monitor_returns_stop_requested_when_stop_file_appears():
    def _write_on_third_poll(n):
        if n == 3:
            stop_request.ORCH_STOP_FILE.write_text("stop")

    pm, _ = _pm()
    proc = _running_proc(on_poll=_write_on_third_poll)

    assert pm._monitor(proc, grace_end_dt=_FAR_FUTURE) == "stop_requested"
    assert proc.poll_calls["n"] == 3
    # Consumed, so the next orchestrator start does not obey it again.
    assert not stop_request.ORCH_STOP_FILE.exists()
    # _monitor only reports: stopping the child is run_session's job.
    proc.terminate.assert_not_called()


def test_monitor_returns_normal_reason_when_child_exits_by_itself():
    pm, _ = _pm()
    assert pm._monitor(_running_proc(polls_before_exit=2, returncode=1),
                       grace_end_dt=_FAR_FUTURE) == "unexpected_exit"
    assert pm._monitor(_running_proc(polls_before_exit=2, returncode=2),
                       grace_end_dt=_FAR_FUTURE) == "ib_disconnected"


def test_monitor_does_not_return_early_without_stop_file():
    pm, _ = _pm()
    proc = _running_proc(polls_before_exit=5, returncode=1)

    assert pm._monitor(proc, grace_end_dt=_FAR_FUTURE) == "unexpected_exit"
    assert proc.poll_calls["n"] == 6  # kept polling until the child really exited


def test_monitor_scheduled_stop_unchanged_without_stop_file():
    pm, _ = _pm()
    past = datetime.datetime(2000, 1, 1, tzinfo=_ET)
    assert pm._monitor(_running_proc(), grace_end_dt=past) == "scheduled_stop"


def test_wait_until_grace_end_returns_stop_requested():
    pm, _ = _pm()
    stop_request.ORCH_STOP_FILE.write_text("stop")
    assert pm._wait_until_grace_end(grace_end_dt=_FAR_FUTURE) == "stop_requested"
    assert not stop_request.ORCH_STOP_FILE.exists()


def test_wait_until_grace_end_returns_none_at_grace_end():
    pm, _ = _pm()
    past = datetime.datetime(2000, 1, 1, tzinfo=_ET)
    assert pm._wait_until_grace_end(grace_end_dt=past) is None


# ---------------------------------------------------------------------------
# ProcessManager.run_session / _request_stop
# ---------------------------------------------------------------------------

def _run_session_with(pm, proc):
    with patch("orchestrator.process._kill_existing_signal_smt"), \
         patch.object(ProcessManager, "_spawn", return_value=proc), \
         patch.object(ProcessManager, "_monitor", return_value="stop_requested"):
        return pm.run_session(datetime.date(2026, 4, 21), grace_end_dt=_FAR_FUTURE)


def test_run_session_stop_request_lets_child_exit_by_itself():
    pm, lines = _pm()
    proc = MagicMock()
    state = {"alive": True, "asked": False}

    def _wait(timeout=None):
        state["asked"] = stop_request.AUTOMATION_STOP_FILE.exists()
        state["alive"] = False
        return 0

    proc.wait.side_effect = _wait
    proc.poll.side_effect = lambda: None if state["alive"] else 0

    assert _run_session_with(pm, proc) == "stop_requested"
    assert state["asked"], "the child's stop file must exist while it is waited on"
    proc.wait.assert_called_once_with(timeout=stop_request.AUTOMATION_STOP_WAIT_S)
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()
    assert not stop_request.AUTOMATION_STOP_FILE.exists()
    assert "Emergency termination" not in "".join(lines)


def test_run_session_stop_request_falls_back_to_terminate_on_timeout():
    pm, lines = _pm()
    proc = MagicMock()
    state = {"alive": True}

    def _wait(timeout=None):
        if state["alive"] and not proc.terminate.called:
            raise subprocess.TimeoutExpired(cmd="automation.main", timeout=timeout)
        return 0

    def _terminate():
        state["alive"] = False

    proc.wait.side_effect = _wait
    proc.terminate.side_effect = _terminate
    proc.poll.side_effect = lambda: None if state["alive"] else 1

    assert _run_session_with(pm, proc) == "stop_requested"
    proc.terminate.assert_called_once()
    assert "not honoured in time" in "".join(lines)
    assert not stop_request.AUTOMATION_STOP_FILE.exists()


def test_run_session_stop_request_terminates_a_child_that_does_not_poll():
    """signal_smt.py has no stop-request watcher: no file, no wait, straight to terminate."""
    pm, _ = _pm(script=Path("signal_smt.py"))
    proc = MagicMock()
    proc.wait.return_value = 0
    written = []
    proc.terminate.side_effect = lambda: written.append(
        stop_request.AUTOMATION_STOP_FILE.exists())

    assert _run_session_with(pm, proc) == "stop_requested"
    proc.terminate.assert_called_once()
    assert written == [False]


# ---------------------------------------------------------------------------
# orchestrator.main.run — which post-session steps run on an operator stop
# ---------------------------------------------------------------------------

def _dt(hour, minute=0):
    return datetime.datetime(2026, 4, 21, hour, minute, tzinfo=_ET)


def _run_orchestrator(tmp_path, monkeypatch, result):
    import orchestrator.main as om
    import paths

    monkeypatch.setattr(om, "_kill_stale_orchestrator", lambda: None)
    monkeypatch.setattr(om, "_check_ib_reachable", lambda *a, **kw: None)
    monkeypatch.setattr(om, "_kill_automation_main", MagicMock())
    pid_file = paths.general_live_dir() / "orchestrator.pid"
    pid_file.write_text(str(os.getpid()))

    pm_instance = MagicMock()
    pm_instance.run_session.return_value = result
    merge = MagicMock()
    write_tsv = MagicMock()
    # LIVE_TRADING: the signal-mode branch would sleep until the pre-session IB shutdown.
    with patch("orchestrator.main._check_parquet_files"), \
         patch("orchestrator.main.LIVE_TRADING", True), \
         patch("orchestrator.main._pre_session_init"), \
         patch("orchestrator.main._SESSIONS_DIR", tmp_path / "sessions"), \
         patch("orchestrator.main.get_et_now", return_value=_dt(9, 25)), \
         patch("orchestrator.main.is_session_open_day", return_value=True), \
         patch("orchestrator.main.ProcessManager", return_value=pm_instance), \
         patch("orchestrator.main._close_session_position") as close_pos, \
         patch("orchestrator.main._start_pre_session_ib",
               return_value=(None, None, [None])) as start_pre, \
         patch("data.parquet_maintenance.merge_session_1s_parquets", merge), \
         patch("orchestrator.relay.SessionRelay.write_trades_tsv", write_tsv):
        with pytest.raises(SystemExit) as exc:
            om.run(summarizer=MagicMock())
    return {"exit": exc.value.code, "merge": merge, "close": close_pos, "tsv": write_tsv,
            "start_pre": start_pre, "pid_file": pid_file, "kill_auto": om._kill_automation_main}


def test_run_stop_requested_exits_through_the_normal_path(tmp_path, monkeypatch):
    r = _run_orchestrator(tmp_path, monkeypatch, "stop_requested")

    assert r["exit"] == 0
    r["tsv"].assert_called_once()              # the session record is kept
    r["merge"].assert_not_called()             # deferred to the next start
    r["close"].assert_not_called()             # the position is NOT closed by a stop
    r["start_pre"].assert_not_called()         # no overnight accumulator: we are exiting
    r["kill_auto"].assert_called_once()        # run()'s finally
    assert not r["pid_file"].exists()          # ... which also removes the pid file


def test_run_ib_disconnected_path_is_unchanged(tmp_path, monkeypatch):
    r = _run_orchestrator(tmp_path, monkeypatch, "ib_disconnected")

    assert r["exit"] == 3
    r["merge"].assert_called_once()
    r["close"].assert_called_once()
    r["tsv"].assert_called_once()


# ---------------------------------------------------------------------------
# automation.main — the stop-request watcher
# ---------------------------------------------------------------------------

def test_watch_stop_request_stops_the_source(tmp_path, capsys):
    import automation.main as am

    stop_file = tmp_path / "automation_stop.req"
    stop_file.write_text("stop")
    source = MagicMock()

    assert am._watch_stop_request(source, stop_file=stop_file, poll_s=0.001) is True
    source.stop.assert_called_once()
    assert "Stop requested" in capsys.readouterr().out


def test_watch_stop_request_uses_the_shared_stop_file():
    import automation.main as am

    stop_request.AUTOMATION_STOP_FILE.write_text("stop")
    source = MagicMock()
    assert am._watch_stop_request(source, poll_s=0.001) is True
    source.stop.assert_called_once()


def test_watch_stop_request_waits_until_the_file_appears(tmp_path):
    import automation.main as am

    stop_file = tmp_path / "automation_stop.req"
    source = MagicMock()
    result = []
    t = threading.Thread(target=lambda: result.append(
        am._watch_stop_request(source, stop_file=stop_file, poll_s=0.001)))
    t.start()
    t.join(timeout=0.1)
    assert t.is_alive(), "must keep waiting while no stop file exists"
    source.stop.assert_not_called()

    stop_file.write_text("stop")
    t.join(timeout=5)
    assert result == [True]
    source.stop.assert_called_once()


def test_watch_stop_request_cancel_ends_the_wait_without_stopping(tmp_path):
    import automation.main as am

    cancel = threading.Event()
    cancel.set()
    source = MagicMock()
    assert am._watch_stop_request(source, stop_file=tmp_path / "none.req",
                                  poll_s=0.001, cancel=cancel) is False
    source.stop.assert_not_called()


def test_watch_stop_request_survives_a_failing_stop(tmp_path, capsys):
    import automation.main as am

    stop_file = tmp_path / "automation_stop.req"
    stop_file.write_text("stop")
    source = MagicMock()
    source.stop.side_effect = RuntimeError("ib gone")

    assert am._watch_stop_request(source, stop_file=stop_file, poll_s=0.001) is True
    assert "ib gone" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# trade.py — shared stop-file path
# ---------------------------------------------------------------------------

def _import_trade(monkeypatch, mock_lo=None):
    monkeypatch.setitem(sys.modules, "live_orders", mock_lo or MagicMock())
    monkeypatch.setitem(sys.modules, "smt_state", MagicMock())
    sys.modules.pop("trade", None)
    return importlib.import_module("trade")


def _tracked_orchestrator(monkeypatch, trade, pid=995001, wait=None):
    """Put `pid` in orchestrator.pid and make psutil.Process(pid) a mock in THIS worktree."""
    import paths

    pid_file = paths.general_live_dir() / "orchestrator.pid"
    pid_file.write_text(str(pid))
    p = MagicMock()
    p.pid = pid
    p.cwd.return_value = str(Path(trade.__file__).resolve().parent)
    if wait is not None:
        p.wait.side_effect = wait
    monkeypatch.setattr("psutil.Process", lambda _pid: p)
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [])
    return p, pid_file


def test_stop_file_is_one_absolute_definition_shared_by_writer_and_readers():
    """The real (unredirected) definition: absolute, anchored to the worktree root, and the
    ONLY one — orchestrator.main, ProcessManager, automation.main and trade.py all read the
    module at call time instead of holding a path of their own."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_stop_request_pristine",
                                                  stop_request.__file__)
    pristine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pristine)
    root = Path(stop_request.__file__).resolve().parent.parent
    assert pristine.ORCH_STOP_FILE == root / "orchestrator_stop.req"
    assert pristine.AUTOMATION_STOP_FILE == root / "automation_stop.req"
    assert pristine.ORCH_STOP_FILE.is_absolute()
    assert pristine.AUTOMATION_STOP_FILE.is_absolute()

    import automation.main as am
    import orchestrator.main as om
    import orchestrator.process as op
    assert om._stop_request is stop_request
    assert op._stop_request is stop_request
    assert am._stop_request is stop_request
    for source in (Path(om.__file__), Path(op.__file__), Path(am.__file__),
                   root / "trade.py"):
        text = source.read_text(encoding="utf-8")
        assert "_stop.req" not in text, f"{source.name} defines its own stop-file path"


def test_terminate_writes_the_stop_file_the_orchestrator_reads_from_another_cwd(
        tmp_path, monkeypatch):
    import orchestrator.main as om

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    trade = _import_trade(monkeypatch)
    seen = {}

    def _wait(timeout=None):
        # What the ORCHESTRATOR would see at this moment, through its own reader.
        try:
            om._check_stop_requested()
            seen["orchestrator_saw_it"] = False
        except om._GracefulStop:
            seen["orchestrator_saw_it"] = True
        seen["timeout"] = timeout
        return 0

    p, _ = _tracked_orchestrator(monkeypatch, trade, wait=_wait)

    killed = trade._terminate_all()

    assert seen["orchestrator_saw_it"] is True
    assert seen["timeout"] == trade._ORCH_STOP_TIMEOUT_S
    assert not (cwd / "orchestrator_stop.req").exists(), "must not be relative to the cwd"
    assert any("graceful" in k for k in killed)
    p.kill.assert_not_called()


def test_orchestrator_stop_timeout_covers_the_in_session_stop_path():
    import orchestrator.process as op
    trade = importlib.import_module("trade")
    # stop-file poll + automation.main's own stop + the kill fallback and descendant reap.
    worst_case = (op._POLL_INTERVAL_S + stop_request.AUTOMATION_STOP_WAIT_S
                  + 2 * op._SIGTERM_WAIT_S)
    assert trade._ORCH_STOP_TIMEOUT_S > worst_case


# ---------------------------------------------------------------------------
# trade.py — stale pid file / leftover stop file
# ---------------------------------------------------------------------------

def test_force_kill_removes_stale_pid_file(monkeypatch):
    trade = _import_trade(monkeypatch)

    def _never_exits(timeout=None):
        raise psutil.TimeoutExpired(timeout)

    p, pid_file = _tracked_orchestrator(monkeypatch, trade, wait=_never_exits)

    killed = trade._terminate_all()

    p.kill.assert_called_once()
    assert not pid_file.exists()
    assert not stop_request.ORCH_STOP_FILE.exists(), "a leftover request stops the next start"
    assert any("force-killed" in k for k in killed)


def test_force_kill_keeps_a_pid_file_rewritten_by_a_newer_orchestrator(monkeypatch):
    trade = _import_trade(monkeypatch)
    holder = {}

    def _never_exits(timeout=None):
        holder["pid_file"].write_text("995999")     # a new orchestrator took over the file
        raise psutil.TimeoutExpired(timeout)

    p, pid_file = _tracked_orchestrator(monkeypatch, trade, wait=_never_exits)
    holder["pid_file"] = pid_file

    trade._terminate_all()

    assert pid_file.read_text() == "995999"


def test_graceful_stop_leaves_pid_file_to_the_orchestrator(monkeypatch):
    trade = _import_trade(monkeypatch)
    p, pid_file = _tracked_orchestrator(monkeypatch, trade, wait=lambda timeout=None: 0)

    killed = trade._terminate_all()

    p.kill.assert_not_called()
    assert pid_file.exists()        # the mock never ran the orchestrator's own `finally`
    assert not stop_request.ORCH_STOP_FILE.exists()
    assert any("graceful" in k for k in killed)


def test_already_dead_orchestrator_pid_file_is_removed(monkeypatch):
    import paths

    trade = _import_trade(monkeypatch)
    pid_file = paths.general_live_dir() / "orchestrator.pid"
    pid_file.write_text("995002")

    def _gone(_pid):
        raise psutil.NoSuchProcess(_pid)

    monkeypatch.setattr("psutil.Process", _gone)
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [])

    killed = trade._terminate_all()

    assert not pid_file.exists()
    assert any("already dead" in k for k in killed)


def test_stop_automation_main_asks_before_killing(monkeypatch):
    trade = _import_trade(monkeypatch)
    proc = MagicMock()
    asked = []

    def _wait(timeout=None):
        asked.append((stop_request.AUTOMATION_STOP_FILE.exists(), timeout))
        return 0

    proc.wait.side_effect = _wait
    proc.is_running.return_value = False

    trade._stop_automation_main(proc)

    assert asked == [(True, stop_request.AUTOMATION_STOP_WAIT_S)]
    proc.terminate.assert_not_called()
    assert not stop_request.AUTOMATION_STOP_FILE.exists()


def test_stop_automation_main_kills_after_the_timeout(monkeypatch):
    trade = _import_trade(monkeypatch)
    proc = MagicMock()
    proc.wait.side_effect = psutil.TimeoutExpired(1)
    proc.is_running.return_value = True

    trade._stop_automation_main(proc)

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert not stop_request.AUTOMATION_STOP_FILE.exists()


# ---------------------------------------------------------------------------
# trade.py terminate — position refusal, override, restart note
# ---------------------------------------------------------------------------

_FLAT = {"active": {}, "stop_entry": "", "stop_direction": ""}
_ACTIVE = {"active": {"direction": "long", "fill_price": 27000.0}, "stop_entry": "",
           "stop_direction": ""}
_PENDING = {"active": {}, "stop_entry": "27000.0", "stop_direction": "up"}


def _terminate(monkeypatch, argv, position, killed=("orchestrator pid=1 (graceful)",)):
    mock_lo = MagicMock()
    mock_lo.get_position.return_value = position
    trade = _import_trade(monkeypatch, mock_lo)
    terminate_all = MagicMock(return_value=list(killed))
    monkeypatch.setattr(trade, "_terminate_all", terminate_all)
    monkeypatch.setattr(sys, "argv", ["trade.py", *argv])
    monkeypatch.delenv("ACT_TRADER", raising=False)
    return trade, terminate_all, mock_lo


def test_terminate_refuses_with_active_position(monkeypatch, capsys):
    trade, terminate_all, _ = _terminate(monkeypatch, ["terminate"], _ACTIVE)
    with pytest.raises(SystemExit) as exc:
        trade.main()
    assert exc.value.code == 1
    terminate_all.assert_not_called()
    out = capsys.readouterr().out
    assert "active position" in out and "refused" in out and "--force" in out
    assert len(out.strip().splitlines()) == 1


def test_terminate_refuses_with_working_order(monkeypatch, capsys):
    trade, terminate_all, _ = _terminate(monkeypatch, ["terminate"], _PENDING)
    with pytest.raises(SystemExit) as exc:
        trade.main()
    assert exc.value.code == 1
    terminate_all.assert_not_called()
    out = capsys.readouterr().out
    assert "working stop entry" in out and "--force" in out


def test_terminate_proceeds_when_flat(monkeypatch, capsys):
    trade, terminate_all, _ = _terminate(monkeypatch, ["terminate"], _FLAT)
    trade.main()
    terminate_all.assert_called_once()
    out = capsys.readouterr().out
    assert "Killed orchestrator pid=1 (graceful)" in out
    assert "WARNING" not in out and "ERROR" not in out


@pytest.mark.parametrize("flag", ["--force", "-f"])
@pytest.mark.parametrize("position,what", [(_ACTIVE, "an active position"),
                                           (_PENDING, "a working stop entry")])
def test_terminate_force_proceeds_and_warns(monkeypatch, capsys, flag, position, what):
    trade, terminate_all, _ = _terminate(monkeypatch, ["terminate", flag], position)
    trade.main()
    terminate_all.assert_called_once()
    out = capsys.readouterr().out
    assert f"WARNING: terminating with {what}" in out
    assert "embedded stop" in out and "13:00" in out
    assert out.index("WARNING") < out.index("Killed")


def test_terminate_never_writes_position(monkeypatch):
    trade, _, mock_lo = _terminate(monkeypatch, ["terminate", "--force"], _ACTIVE)
    trade.main()
    called = {c[0] for c in mock_lo.method_calls}
    assert called == {"get_position"}


def test_terminate_nothing_to_terminate_prints_no_note(monkeypatch, capsys):
    trade, _, _ = _terminate(monkeypatch, ["terminate"], _FLAT, killed=())
    trade.main()
    out = capsys.readouterr().out
    assert "Nothing to terminate" in out and "NOTE" not in out


def _session_dir(trade):
    d = trade._agent_session_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_terminate_notes_a_restart_stays_dark_when_a_plan_is_on_disk(monkeypatch, capsys):
    trade, _, _ = _terminate(monkeypatch, ["terminate"], _FLAT)
    (_session_dir(trade) / "plans.json").write_text(
        json.dumps({"p1": {"plan_id": "p1"}}), encoding="utf-8")
    trade.main()
    out = capsys.readouterr().out
    assert "DISARMED (reason `restart`)" in out
    assert "dark for the rest of the session" in out


def test_restart_note_matches_what_the_graft_does(monkeypatch, tmp_path):
    """The note is worded from TraderGraft's own rule: a live start (order sink present)
    that finds a plan in plans.json disarms with reason `restart`."""
    from agent.trader.graft import TraderGraft
    from agent.trader.plan_store import PlanStore

    PlanStore(tmp_path).put({"plan_id": "p1"})
    assert TraderGraft(tmp_path, None, order_sink=lambda *a, **k: None).disarmed() == "restart"
    assert TraderGraft(tmp_path, None).disarmed() is None


def test_terminate_note_without_a_plan(monkeypatch, capsys):
    trade, _, _ = _terminate(monkeypatch, ["terminate"], _FLAT)
    trade.main()
    out = capsys.readouterr().out
    assert "no agent plan on disk" in out
    assert "DISARMED" not in out


def test_terminate_note_silent_when_legacy_owns_the_dispatcher(monkeypatch, capsys):
    trade, _, _ = _terminate(monkeypatch, ["terminate"], _FLAT)
    monkeypatch.setenv("ACT_TRADER", "0")
    trade.main()
    assert "NOTE" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# trade.py start — pause state
# ---------------------------------------------------------------------------

def _start(monkeypatch, tmp_path, argv, *, paused):
    import subprocess as _subp
    import time as _time

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_subp, "Popen", lambda *a, **k: MagicMock())
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)
    mock_lo = MagicMock()
    mock_lo.is_paused.return_value = paused
    mock_lo.pause.return_value = not paused
    mock_lo.resume.return_value = paused
    trade = _import_trade(monkeypatch, mock_lo)
    monkeypatch.setattr(trade, "_orchestrator_pid", lambda: None)
    monkeypatch.setattr(trade, "_terminate_all", MagicMock(return_value=[]))
    monkeypatch.setattr(sys, "argv", ["trade.py", "start", *argv])
    trade.main()
    return mock_lo


def test_start_prints_a_carried_over_pause(monkeypatch, tmp_path, capsys):
    mock_lo = _start(monkeypatch, tmp_path, [], paused=True)
    out = capsys.readouterr().out
    assert "Pause state: PAUSED (carried over)" in out
    assert "trade.py resume" in out
    mock_lo.pause.assert_not_called()
    mock_lo.resume.assert_not_called()


def test_start_says_not_paused_without_the_sentinel(monkeypatch, tmp_path, capsys):
    mock_lo = _start(monkeypatch, tmp_path, [], paused=False)
    out = capsys.readouterr().out
    assert "Pause state: not paused" in out
    assert "PAUSED" not in out
    mock_lo.pause.assert_not_called()
    mock_lo.resume.assert_not_called()


def test_start_resume_behaviour_unchanged(monkeypatch, tmp_path, capsys):
    mock_lo = _start(monkeypatch, tmp_path, ["--resume"], paused=True)
    out = capsys.readouterr().out
    mock_lo.resume.assert_called_once()
    mock_lo.pause.assert_not_called()
    assert "Starting in resumed mode" in out
    assert "Pause state" not in out


def test_start_pause_behaviour_unchanged(monkeypatch, tmp_path, capsys):
    mock_lo = _start(monkeypatch, tmp_path, ["--pause"], paused=False)
    out = capsys.readouterr().out
    mock_lo.pause.assert_called_once()
    mock_lo.resume.assert_not_called()
    assert "Starting in paused mode" in out
    assert "Pause state" not in out
