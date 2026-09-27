"""automation/profiling.py (`--profile`): baseline snapshot at 09:15:30 ET, final snapshot
(with growth) at 13:05:30 or on a terminate request -- exactly once; never raises; writes
only under <out>/profile/ plus the handshake files."""
import datetime
import time
import tracemalloc

import pytest

from automation import profiling

ET = profiling._ET


def _at(hh, mm, ss=0):
    return datetime.datetime(2026, 9, 28, hh, mm, ss, tzinfo=ET)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(profiling, "_started", False)
    monkeypatch.setattr(profiling, "_stop", profiling.threading.Event())
    monkeypatch.setattr(profiling, "SAMPLE_SEC", 0.05)
    monkeypatch.setattr(profiling, "POLL_SEC", 0.02)
    monkeypatch.setattr(profiling, "ROOT", tmp_path / "root")
    monkeypatch.setattr(profiling, "REQUEST_FILE", tmp_path / "root" / "profile_snapshot.req")
    (tmp_path / "root").mkdir()
    yield
    profiling._stop.set()
    time.sleep(0.15)
    if tracemalloc.is_tracing():
        tracemalloc.stop()


def _clock_at(monkeypatch, when):
    monkeypatch.setattr(profiling, "_clock", lambda: when)


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.03)
    return False


def _mem_lines(folder):
    f = next(folder.glob("mem_automation_*.tsv"), None)
    return f.read_text(encoding="utf-8").splitlines() if f else []


def _snap(folder, tag):
    f = next(folder.glob(f"tracemalloc_automation_*_{tag}.txt"), None)
    return f.read_text(encoding="utf-8") if f else None


def _start_at_0900_and_take_baseline(monkeypatch, tmp_path):
    _clock_at(monkeypatch, _at(9, 0))
    folder = profiling.start("automation", tmp_path)
    _clock_at(monkeypatch, _at(9, 15, 30))
    assert _wait_for(lambda: _snap(folder, "0915") is not None)
    return folder


def _request():
    profiling.REQUEST_FILE.write_text("snapshot", encoding="utf-8")


def test_pre_window_start_traces_and_samples_memory(monkeypatch, tmp_path):
    _clock_at(monkeypatch, _at(9, 0))
    folder = profiling.start("automation", tmp_path)
    assert folder == tmp_path / "profile" and tracemalloc.is_tracing()
    assert _wait_for(lambda: len(_mem_lines(folder)) >= 3)
    lines = _mem_lines(folder)
    assert lines[0].split("\t")[:3] == ["time_et", "rss_mb", "private_mb"]
    assert int(lines[1].split("\t")[1]) > 0
    assert not list(folder.glob("tracemalloc_*"))


def test_baseline_at_0915_30_keeps_tracing_through_the_window(monkeypatch, tmp_path):
    folder = _start_at_0900_and_take_baseline(monkeypatch, tmp_path)
    assert "baseline" in _snap(folder, "0915") and "## top" in _snap(folder, "0915")
    _clock_at(monkeypatch, _at(11, 0))
    time.sleep(0.3)
    assert tracemalloc.is_tracing()
    assert _snap(folder, "final") is None


def test_0915_00_sample_does_not_take_the_baseline_early(monkeypatch, tmp_path):
    _clock_at(monkeypatch, _at(9, 0))
    profiling.start("automation", tmp_path)
    _clock_at(monkeypatch, _at(9, 15, 0))
    time.sleep(0.3)
    assert tracemalloc.is_tracing()
    assert not list((tmp_path / "profile").glob("tracemalloc_*"))


def test_final_at_1305_30_reports_growth_then_stops_tracing(monkeypatch, tmp_path):
    folder = _start_at_0900_and_take_baseline(monkeypatch, tmp_path)
    _clock_at(monkeypatch, _at(13, 0, 0))              # the window-end close: no snapshot
    time.sleep(0.3)
    assert _snap(folder, "final") is None
    _clock_at(monkeypatch, _at(13, 5, 30))
    assert _wait_for(lambda: _snap(folder, "final") is not None)
    assert "growth since the 09:15:30 baseline" in _snap(folder, "final")
    assert _wait_for(lambda: not tracemalloc.is_tracing())


def test_terminate_request_takes_the_final_snapshot_and_confirms(monkeypatch, tmp_path):
    folder = _start_at_0900_and_take_baseline(monkeypatch, tmp_path)
    _clock_at(monkeypatch, _at(11, 30))
    _request()
    assert _wait_for(lambda: profiling.done_file("automation").exists())
    text = _snap(folder, "final")
    assert "terminate requested" in text and "growth since the 09:15:30 baseline" in text
    assert not tracemalloc.is_tracing()


def test_terminate_after_the_1305_final_does_not_snapshot_again(monkeypatch, tmp_path):
    folder = _start_at_0900_and_take_baseline(monkeypatch, tmp_path)
    _clock_at(monkeypatch, _at(13, 5, 30))
    assert _wait_for(lambda: _snap(folder, "final") is not None)
    first = _snap(folder, "final")
    time.sleep(0.05)
    _clock_at(monkeypatch, _at(14, 0))
    _request()
    assert _wait_for(lambda: profiling.done_file("automation").exists())
    assert _snap(folder, "final") == first
    assert len(list(folder.glob("tracemalloc_*_final.txt"))) == 1


def test_a_stale_request_from_before_start_is_ignored(monkeypatch, tmp_path):
    _request()
    old = time.time() - 60
    import os
    os.utime(profiling.REQUEST_FILE, (old, old))
    _clock_at(monkeypatch, _at(9, 0))
    profiling.start("automation", tmp_path)
    time.sleep(0.3)
    assert tracemalloc.is_tracing()
    assert not profiling.done_file("automation").exists()


@pytest.mark.parametrize("when", [(9, 15, 0), (9, 30, 0), (12, 59, 59)])
def test_start_inside_the_window_never_traces_but_still_confirms(monkeypatch, tmp_path, when):
    _clock_at(monkeypatch, _at(*when))
    folder = profiling.start("automation", tmp_path)
    assert folder is not None and not tracemalloc.is_tracing()
    assert "not traced" in next(folder.glob("tracemalloc_*_note.txt")).read_text(encoding="utf-8")
    _request()
    assert _wait_for(lambda: profiling.done_file("automation").exists())
    assert _snap(folder, "final") is None


def test_missed_baseline_stops_tracing_at_the_deadline(monkeypatch, tmp_path):
    _clock_at(monkeypatch, _at(9, 0))
    folder = profiling.start("automation", tmp_path)
    _clock_at(monkeypatch, _at(9, 19, 0))
    assert _wait_for(lambda: not tracemalloc.is_tracing())
    assert "no baseline" in next(folder.glob("tracemalloc_*_note.txt")).read_text(encoding="utf-8")


def test_next_mark_is_the_wall_clock_00_or_30(monkeypatch):
    monkeypatch.setattr(profiling, "SAMPLE_SEC", 30)
    assert profiling._to_next_mark(_at(9, 15, 12)) == pytest.approx(18)
    assert profiling._to_next_mark(_at(9, 15, 30)) == pytest.approx(30)
    assert profiling._to_next_mark(_at(9, 15, 59)) == pytest.approx(1)


def test_second_start_in_the_same_process_is_a_no_op(monkeypatch, tmp_path):
    _clock_at(monkeypatch, _at(9, 0))
    assert profiling.start("automation", tmp_path / "a") is not None
    assert profiling.start("automation", tmp_path / "b") is None
    assert not (tmp_path / "b").exists()


def test_an_unusable_output_dir_disables_profiling_instead_of_raising(monkeypatch, tmp_path):
    _clock_at(monkeypatch, _at(9, 0))
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    assert profiling.start("automation", blocker) is None
    assert profiling._started is False
    assert not tracemalloc.is_tracing()
