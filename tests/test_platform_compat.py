# tests/test_platform_compat.py
# platform_compat is the single OS switch (plan 52). The platform is simulated, so these
# run identically on Windows, macOS and Linux.

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

import platform_compat as pc


def _fake_os(name: str):
    return types.SimpleNamespace(name=name, getpid=os.getpid)


@pytest.mark.parametrize("name", ["python", "Python", "python.exe", "python3", "python3.12",
                                  "python3.12.1", "pythonw.exe"])
def test_is_python_process_name_accepts_python_variants(name):
    assert pc.is_python_process_name(name) is True


@pytest.mark.parametrize("name", ["uv", "uv.exe", "powershell.exe", "pythonista", "bash", "", None])
def test_is_python_process_name_rejects_non_python(name):
    assert pc.is_python_process_name(name) is False


def test_detached_kwargs_windows_is_create_no_window(monkeypatch):
    monkeypatch.setattr(pc, "os", _fake_os("nt"))
    assert pc.detached_popen_kwargs() == {"creationflags": 0x08000000}


def test_detached_kwargs_posix_is_start_new_session(monkeypatch):
    monkeypatch.setattr(pc, "os", _fake_os("posix"))
    kw = pc.detached_popen_kwargs()
    assert kw == {"start_new_session": True}
    assert "creationflags" not in kw


def test_prevent_idle_sleep_windows_calls_execution_state_once_and_swallows_errors(monkeypatch):
    import ctypes
    calls = []

    def _set(flags):
        calls.append(flags)
        raise OSError("boom")

    fake_windll = types.SimpleNamespace(kernel32=types.SimpleNamespace(SetThreadExecutionState=_set))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(pc, "os", _fake_os("nt"))
    assert pc.prevent_idle_sleep() is None
    assert calls == [0x80000001]


def test_prevent_idle_sleep_macos_spawns_caffeinate_tied_to_pid(monkeypatch):
    seen = {}
    sentinel = object()

    def _popen(argv, **kw):
        seen["argv"] = argv
        return sentinel

    monkeypatch.setattr(pc, "os", _fake_os("posix"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _popen)
    assert pc.prevent_idle_sleep(4242) is sentinel
    assert seen["argv"] == ["caffeinate", "-dimsu", "-w", "4242"]
    assert pc.prevent_idle_sleep() is sentinel
    assert seen["argv"] == ["caffeinate", "-dimsu", "-w", str(os.getpid())]


def test_prevent_idle_sleep_macos_caffeinate_missing_prints_one_line_and_returns_none(monkeypatch, capsys):
    def _popen(argv, **kw):
        raise FileNotFoundError("caffeinate")

    monkeypatch.setattr(pc, "os", _fake_os("posix"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _popen)
    assert pc.prevent_idle_sleep() is None
    err = capsys.readouterr().err
    assert err.strip().splitlines() == ["macOS sleep prevention unavailable"]


def test_prevent_idle_sleep_other_posix_is_noop(monkeypatch):
    def _popen(*a, **k):
        raise AssertionError("must not spawn")

    monkeypatch.setattr(pc, "os", _fake_os("posix"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", _popen)
    assert pc.prevent_idle_sleep() is None
