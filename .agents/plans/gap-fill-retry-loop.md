# Gap-Fill Retry Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production gap-fill (`IbRealtimeSource.start()`, used by both the orchestrator and `automation.main`) and offline gap-fill (`trade.py gap-fill`) behave identically — both fully close whatever historical data gap exists, however long that takes, by looping the existing single-pass fill with ~10-11 min spacing to respect IB's historical-data pacing limit. Production only gives up (exits with a distinct code) after 5 consecutive rounds that raise a real error — never merely because the gap is large.

**Architecture:** One new shared function, `run_gap_fill_with_retries()`, wraps the existing single-pass `IbRealtimeSource.gap_fill()` in a retry loop. Both the offline path (`gap_fill.gap_fill_until_now()`) and the production path (`IbRealtimeSource.start()`) call this same function — no subprocess, no duplicated logic. A new `GapFillFailedError` exception, raised after 5 consecutive round failures, propagates to `automation/main.py` and `orchestrator/main.py`, each of which already has an established pattern for catching a specific `IbRealtimeSource`-raised exception and exiting with a dedicated code (mirrors the existing `IbGatewayDisconnectedError` → `sys.exit(2)` pattern).

**Tech Stack:** Python 3.12, pandas, ib_insync, pytest, unittest.mock.

## Global Constraints

- `_GAP_HEADSUP_HOURS = 24` — informational threshold; a gap larger than this prints a one-time "this may take a while" message.
- `_GAP_CAUGHT_UP_HOURS = 1` — the loop's convergence target; matches what was validated manually on 2026-07-10.
- `round_spacing_s = 650.0` (~10.8 min) — spacing between rounds; matches what was validated manually to reliably clear IB's rolling 10-min pacing window.
- `max_consecutive_failures = 5` — only rounds that raise an exception count; a round that completes without error but leaves the gap open is not a failure.
- New process exit code `11` for `GapFillFailedError`, distinct from the existing `2` (IB disconnect, `automation/main.py`) and `10` (missing parquets, `orchestrator/main.py`).
- No new external notification channel (Slack/email/push) — this repo has none today. Notification is a loud log message + the distinct exit code, matching the existing `--check-parquets` → `sys.exit(10)` precedent.
- `IbRealtimeSource.gap_fill()` (the single-pass primitive: one 1s fill attempt + one 1m fill attempt) is **never modified** in this plan — it is passed as a callable into the new retry wrapper, unchanged.

---

### Task 1: Add `gap_hours_by_file()` and `GapFillFailedError` to `data/ib_realtime.py`

**Files:**
- Modify: `data/ib_realtime.py:66-68` (insert after `IbGatewayDisconnectedError`, before `class IbRealtimeSource:`)
- Test: `tests/test_gap_fill.py` (append new section at end of file)

**Interfaces:**
- Produces: `gap_hours_by_file(bar_data_dir: Path) -> dict[str, float]` — keys are `"MNQ_1s.parquet"`, `"MES_1s.parquet"`, `"MNQ_1m.parquet"`, `"MES_1m.parquet"`; values are hours since now (`float("inf")` if the file is missing, empty, or unreadable).
- Produces: `class GapFillFailedError(Exception)` with attributes `.gaps_hours: dict[str, float]` and `.last_error: Exception`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gap_fill.py`:

```python
# ---------------------------------------------------------------------------
# gap_hours_by_file
# ---------------------------------------------------------------------------

def _write_bar(path, ts):
    df = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([ts]),
    )
    df.to_parquet(path)


def test_gap_hours_by_file_all_current(tmp_path):
    from data.ib_realtime import gap_hours_by_file
    now = pd.Timestamp.now(tz="America/New_York")
    for name in ("MNQ_1s.parquet", "MES_1s.parquet", "MNQ_1m.parquet", "MES_1m.parquet"):
        _write_bar(tmp_path / name, now)

    gaps = gap_hours_by_file(tmp_path)

    assert set(gaps) == {"MNQ_1s.parquet", "MES_1s.parquet", "MNQ_1m.parquet", "MES_1m.parquet"}
    assert all(0.0 <= h < 0.01 for h in gaps.values())


def test_gap_hours_by_file_missing_file_is_inf(tmp_path):
    from data.ib_realtime import gap_hours_by_file
    now = pd.Timestamp.now(tz="America/New_York")
    _write_bar(tmp_path / "MNQ_1s.parquet", now)
    # MES_1s.parquet, MNQ_1m.parquet, MES_1m.parquet intentionally absent

    gaps = gap_hours_by_file(tmp_path)

    assert gaps["MNQ_1s.parquet"] < 0.01
    assert gaps["MES_1s.parquet"] == float("inf")
    assert gaps["MNQ_1m.parquet"] == float("inf")
    assert gaps["MES_1m.parquet"] == float("inf")


def test_gap_hours_by_file_empty_df_is_inf(tmp_path):
    from data.ib_realtime import gap_hours_by_file
    empty = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([]),
    )
    empty.to_parquet(tmp_path / "MNQ_1s.parquet")

    gaps = gap_hours_by_file(tmp_path)

    assert gaps["MNQ_1s.parquet"] == float("inf")


# ---------------------------------------------------------------------------
# GapFillFailedError
# ---------------------------------------------------------------------------

def test_gap_fill_failed_error_carries_gaps_and_last_error():
    from data.ib_realtime import GapFillFailedError
    last_error = RuntimeError("ib broke")
    err = GapFillFailedError({"MNQ_1s.parquet": 50.0}, last_error)

    assert err.gaps_hours == {"MNQ_1s.parquet": 50.0}
    assert err.last_error is last_error
    assert "50.0" in str(err) or "50" in str(err)
```

Add `import pandas as pd` to the top of `tests/test_gap_fill.py` if not already present (check first — current imports are `from unittest.mock import MagicMock`, `import pytest`, `import gap_fill`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gap_fill.py -v -k "gap_hours_by_file or gap_fill_failed_error"`
Expected: FAIL with `ImportError: cannot import name 'gap_hours_by_file'` (and similarly for `GapFillFailedError`).

- [ ] **Step 3: Implement `gap_hours_by_file` and `GapFillFailedError`**

In `data/ib_realtime.py`, insert immediately after the `IbGatewayDisconnectedError` class (currently lines 66-67, right before `class IbRealtimeSource:` at line 70):

```python
_GAP_FILE_NAMES = ["MNQ_1s.parquet", "MES_1s.parquet", "MNQ_1m.parquet", "MES_1m.parquet"]
_GAP_HEADSUP_HOURS = 24    # informational: gap this large likely needs multiple rounds
_GAP_CAUGHT_UP_HOURS = 1   # loop's convergence target


def gap_hours_by_file(bar_data_dir: Path) -> dict:
    """Hours between now and the last bar in each of the 4 main parquets.

    A missing, empty, or unreadable file returns float('inf') for that entry rather
    than raising — a first-ever run with no existing parquet naturally reads as
    "needs multiple rounds," consistent with how the existing fill logic already
    creates files from scratch when absent.
    """
    now = pd.Timestamp.now(tz="UTC")
    gaps: dict = {}
    for name in _GAP_FILE_NAMES:
        path = bar_data_dir / name
        if not path.exists():
            gaps[name] = float("inf")
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:
            gaps[name] = float("inf")
            continue
        if df.empty:
            gaps[name] = float("inf")
            continue
        last = df.index[-1]
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        gaps[name] = (now - last.tz_convert("UTC")).total_seconds() / 3600.0
    return gaps


class GapFillFailedError(Exception):
    """Raised when gap-fill hits 5 consecutive round failures without closing the gap."""

    def __init__(self, gaps_hours: dict, last_error: Exception) -> None:
        self.gaps_hours = gaps_hours
        self.last_error = last_error
        super().__init__(
            f"gap-fill failed 5 consecutive rounds; last error: {last_error}; "
            f"gaps: {gaps_hours}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gap_fill.py -v -k "gap_hours_by_file or gap_fill_failed_error"`
Expected: PASS (5 tests: `test_gap_hours_by_file_all_current`, `test_gap_hours_by_file_missing_file_is_inf`, `test_gap_hours_by_file_empty_df_is_inf`, `test_gap_fill_failed_error_carries_gaps_and_last_error`)

- [ ] **Step 5: Run the full test_gap_fill.py suite to check for regressions**

Run: `uv run pytest tests/test_gap_fill.py -v`
Expected: All PASS (no existing tests touched yet in this task).

- [ ] **Step 6: Commit**

```bash
git add data/ib_realtime.py tests/test_gap_fill.py
git commit -m "feat(gap-fill): add gap_hours_by_file and GapFillFailedError"
```

---

### Task 2: Add `run_gap_fill_with_retries()` to `data/ib_realtime.py`

**Files:**
- Modify: `data/ib_realtime.py` (insert immediately after `GapFillFailedError`, added in Task 1)
- Test: `tests/test_gap_fill.py` (append new section)

**Interfaces:**
- Consumes: `gap_hours_by_file(bar_data_dir: Path) -> dict` and `GapFillFailedError` from Task 1.
- Produces: `run_gap_fill_with_retries(do_one_round: Callable[[], None], bar_data_dir: Path, *, max_consecutive_failures: int = 5, round_spacing_s: float = 650.0) -> None`. Returns when `gap_hours_by_file(bar_data_dir)` shows every file `<= _GAP_CAUGHT_UP_HOURS`. Raises `GapFillFailedError` after `max_consecutive_failures` consecutive rounds where `do_one_round()` raised.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gap_fill.py`:

```python
# ---------------------------------------------------------------------------
# run_gap_fill_with_retries
# ---------------------------------------------------------------------------

def test_run_gap_fill_with_retries_single_round_closes_gap(monkeypatch, tmp_path):
    import data.ib_realtime as _ir

    gap_sequence = [{"MNQ_1s.parquet": 2.0}, {"MNQ_1s.parquet": 0.1}]
    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: gap_sequence.pop(0))
    sleep_mock = MagicMock()
    monkeypatch.setattr(_ir.time, "sleep", sleep_mock)

    round_calls = []
    _ir.run_gap_fill_with_retries(lambda: round_calls.append(1), tmp_path)

    assert round_calls == [1]
    sleep_mock.assert_not_called()


def test_run_gap_fill_with_retries_multiple_rounds_until_caught_up(monkeypatch, tmp_path, capsys):
    import data.ib_realtime as _ir

    gap_sequence = [
        {"MES_1s.parquet": 100.0},  # headsup check (>24h -> message printed)
        {"MES_1s.parquet": 50.0},   # after round 1 (still >1h)
        {"MES_1s.parquet": 20.0},   # after round 2 (still >1h)
        {"MES_1s.parquet": 0.5},    # after round 3 (<=1h -> done)
    ]
    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: gap_sequence.pop(0))
    sleep_mock = MagicMock()
    monkeypatch.setattr(_ir.time, "sleep", sleep_mock)

    round_calls = []
    _ir.run_gap_fill_with_retries(
        lambda: round_calls.append(1), tmp_path, round_spacing_s=650.0
    )

    assert round_calls == [1, 1, 1]
    assert sleep_mock.call_count == 2
    sleep_mock.assert_called_with(650.0)
    assert "Large gap detected" in capsys.readouterr().out


def test_run_gap_fill_with_retries_no_headsup_for_small_initial_gap(monkeypatch, tmp_path, capsys):
    import data.ib_realtime as _ir

    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: {"MNQ_1s.parquet": 0.1})
    monkeypatch.setattr(_ir.time, "sleep", MagicMock())

    _ir.run_gap_fill_with_retries(lambda: None, tmp_path)

    assert "Large gap detected" not in capsys.readouterr().out


def test_run_gap_fill_with_retries_failure_counter_resets_on_success(monkeypatch, tmp_path):
    import data.ib_realtime as _ir

    # 7 rounds: fail, ok (resets counter), then 5 more consecutive fails -> raises on round 7.
    # If the counter did NOT reset after "ok", it would raise on round 6 instead (only 5
    # total fails needed cumulatively) -- asserting call_idx == 7 catches that regression.
    call_sequence = ["fail", "ok", "fail", "fail", "fail", "fail", "fail"]
    call_idx = [0]

    def _do_round():
        outcome = call_sequence[call_idx[0]]
        call_idx[0] += 1
        if outcome == "fail":
            raise RuntimeError("boom")

    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: {"MNQ_1s.parquet": 100.0})
    monkeypatch.setattr(_ir.time, "sleep", MagicMock())

    with pytest.raises(_ir.GapFillFailedError):
        _ir.run_gap_fill_with_retries(_do_round, tmp_path, max_consecutive_failures=5)

    assert call_idx[0] == 7


def test_run_gap_fill_with_retries_raises_after_max_consecutive_failures(monkeypatch, tmp_path):
    import data.ib_realtime as _ir

    monkeypatch.setattr(_ir, "gap_hours_by_file", lambda d: {"MNQ_1s.parquet": 50.0})
    monkeypatch.setattr(_ir.time, "sleep", MagicMock())

    def _always_fail():
        raise RuntimeError("ib broke")

    with pytest.raises(_ir.GapFillFailedError) as exc_info:
        _ir.run_gap_fill_with_retries(_always_fail, tmp_path, max_consecutive_failures=5)

    err = exc_info.value
    assert err.gaps_hours == {"MNQ_1s.parquet": 50.0}
    assert isinstance(err.last_error, RuntimeError)
    assert str(err.last_error) == "ib broke"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gap_fill.py -v -k run_gap_fill_with_retries`
Expected: FAIL with `AttributeError: module 'data.ib_realtime' has no attribute 'run_gap_fill_with_retries'`.

- [ ] **Step 3: Implement `run_gap_fill_with_retries`**

In `data/ib_realtime.py`, insert immediately after the `GapFillFailedError` class added in Task 1:

```python
def run_gap_fill_with_retries(
    do_one_round,
    bar_data_dir: Path,
    *,
    max_consecutive_failures: int = 5,
    round_spacing_s: float = 650.0,
) -> None:
    """Run do_one_round() repeatedly, spaced to respect IB's historical-data pacing
    limit (~60 requests / rolling 10-min window), until all 4 main parquets are
    within _GAP_CAUGHT_UP_HOURS of now.

    Used by both the offline `trade.py gap-fill` path (gap_fill.gap_fill_until_now)
    and the production path (IbRealtimeSource.start()) so both behave identically —
    the only difference is where the loop runs.

    Raises GapFillFailedError after max_consecutive_failures consecutive rounds that
    raise an exception. A round that completes without an exception but leaves the
    gap open is NOT a failure — that is expected for a large gap and simply triggers
    another round with no cap on total round count.
    """
    gaps = gap_hours_by_file(bar_data_dir)
    if max(gaps.values(), default=0.0) > _GAP_HEADSUP_HOURS:
        print(
            f"[gap_fill] Large gap detected ({gaps}) — this may take several rounds "
            f"spaced ~{round_spacing_s / 60:.0f} min apart to respect IB's pacing limit. "
            "Sitting tight...",
            flush=True,
        )

    consecutive_failures = 0
    last_error = None
    round_num = 1
    while True:
        try:
            do_one_round()
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            last_error = exc
            print(
                f"[gap_fill] round {round_num} failed ({exc}) — "
                f"{consecutive_failures}/{max_consecutive_failures} consecutive failures",
                flush=True,
            )
            if consecutive_failures >= max_consecutive_failures:
                raise GapFillFailedError(gap_hours_by_file(bar_data_dir), last_error)

        gaps = gap_hours_by_file(bar_data_dir)
        if max(gaps.values(), default=0.0) <= _GAP_CAUGHT_UP_HOURS:
            print(
                f"[gap_fill] Caught up — all parquets within {_GAP_CAUGHT_UP_HOURS}h of now.",
                flush=True,
            )
            return

        print(
            f"[gap_fill] round {round_num} done — gap remaining: {gaps} — "
            f"next round in ~{round_spacing_s / 60:.0f} min",
            flush=True,
        )
        time.sleep(round_spacing_s)
        round_num += 1
```

(`time` and `Callable` are already imported at the top of `data/ib_realtime.py` — no new imports needed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gap_fill.py -v -k run_gap_fill_with_retries`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full test_gap_fill.py suite to check for regressions**

Run: `uv run pytest tests/test_gap_fill.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add data/ib_realtime.py tests/test_gap_fill.py
git commit -m "feat(gap-fill): add run_gap_fill_with_retries shared retry loop"
```

---

### Task 3: Wire `gap_fill.gap_fill_until_now()` to use `run_gap_fill_with_retries`

**Files:**
- Modify: `gap_fill.py:90`
- Test: `tests/test_gap_fill.py:17-101` (update 2 existing tests)

**Interfaces:**
- Consumes: `run_gap_fill_with_retries` from Task 2.
- Produces: no change to `gap_fill_until_now`'s public signature — `trade.py`'s CLI handler (`trade.py:398-403`) needs no change.

- [ ] **Step 1: Update the existing tests that will break**

`tests/test_gap_fill.py` has two tests that currently assert `source.gap_fill` (a `MagicMock`) was called directly. Once `gap_fill_until_now` calls `run_gap_fill_with_retries(source.gap_fill, bar_data_dir)` instead, calling the real `run_gap_fill_with_retries` against an empty `tmp_path` (no parquet files → `gap_hours_by_file` returns `inf` for everything) would loop forever. Patch `run_gap_fill_with_retries` itself so these tests keep testing what they're meant to test (that `gap_fill_until_now` reaches the source construction + fill invocation) without touching the retry-loop internals (already covered by Task 2's tests).

Replace `test_gap_fill_until_now_runs_reachable_merge_and_source` (currently lines 17-55) with:

```python
def test_gap_fill_until_now_runs_reachable_merge_and_source(monkeypatch, tmp_path):
    """Happy path: reachable check + session merge + fill-only IbRealtimeSource.gap_fill(),
    run through run_gap_fill_with_retries()."""
    monkeypatch.setenv("MNQ_CONID", "111")
    monkeypatch.setenv("MES_CONID", "222")
    monkeypatch.setenv("IB_HOST", "1.2.3.4")
    monkeypatch.setenv("IB_PORT", "4321")
    monkeypatch.setenv("PRE_SESSION_IB_CLIENT_ID", "10")

    reachable = MagicMock()
    monkeypatch.setattr(gap_fill, "check_ib_reachable", reachable)

    merge = MagicMock()
    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "merge_session_1s_parquets", merge)

    instances: list = []
    import data.ib_realtime as _ir

    class _FakeSource:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.gap_fill = MagicMock()
            instances.append(self)

    monkeypatch.setattr(_ir, "IbRealtimeSource", _FakeSource)

    retries_calls: list = []
    monkeypatch.setattr(
        _ir, "run_gap_fill_with_retries",
        lambda fn, d, **kw: retries_calls.append((fn, d)) or fn(),
    )

    gap_fill.gap_fill_until_now(tmp_path)

    reachable.assert_called_once()
    merge.assert_called_once_with(tmp_path)
    assert len(instances) == 1
    src = instances[0]
    src.gap_fill.assert_called_once()
    assert retries_calls == [(src.gap_fill, tmp_path)]
    assert src.kwargs["host"] == "1.2.3.4"
    assert src.kwargs["port"] == 4321
    assert src.kwargs["client_id"] == 10
    assert src.kwargs["mnq_conid"] == "111"
    assert src.kwargs["mes_conid"] == "222"
    assert src.kwargs["bar_data_dir"] == tmp_path
```

Replace `test_gap_fill_until_now_honours_skip_flags` (currently lines 82-101) with:

```python
def test_gap_fill_until_now_honours_skip_flags(monkeypatch, tmp_path):
    """check_reachable=False / merge_sessions=False skip those steps."""
    monkeypatch.setenv("MNQ_CONID", "1")
    monkeypatch.setenv("MES_CONID", "2")

    reachable = MagicMock()
    monkeypatch.setattr(gap_fill, "check_ib_reachable", reachable)
    merge = MagicMock()
    import data.parquet_maintenance as _pm
    monkeypatch.setattr(_pm, "merge_session_1s_parquets", merge)

    import data.ib_realtime as _ir
    fake = MagicMock()
    monkeypatch.setattr(_ir, "IbRealtimeSource", lambda **k: fake)
    monkeypatch.setattr(
        _ir, "run_gap_fill_with_retries",
        lambda fn, d, **kw: fn(),
    )

    gap_fill.gap_fill_until_now(tmp_path, check_reachable=False, merge_sessions=False)

    reachable.assert_not_called()
    merge.assert_not_called()
    fake.gap_fill.assert_called_once()
```

Leave `test_gap_fill_until_now_skips_when_conids_absent` and `test_gap_fill_until_now_defaults_dir_to_general_live` unchanged — neither reaches the `gap_fill`/`run_gap_fill_with_retries` call.

- [ ] **Step 2: Run tests to verify they fail against the current (unmodified) gap_fill.py**

Run: `uv run pytest tests/test_gap_fill.py -v -k "runs_reachable_merge_and_source or honours_skip_flags"`
Expected: FAIL — `retries_calls` stays empty / `fake.gap_fill.assert_called_once()` fails, because `gap_fill_until_now` still calls `source.gap_fill()` directly, never touching `run_gap_fill_with_retries`.

- [ ] **Step 3: Wire `gap_fill_until_now`**

In `gap_fill.py`, replace line 90:

```python
    source.gap_fill()
```

with:

```python
    from data.ib_realtime import run_gap_fill_with_retries
    run_gap_fill_with_retries(source.gap_fill, bar_data_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gap_fill.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add gap_fill.py tests/test_gap_fill.py
git commit -m "feat(gap-fill): route offline gap-fill through run_gap_fill_with_retries"
```

---

### Task 4: Wire `IbRealtimeSource.start()` to use `run_gap_fill_with_retries`

**Files:**
- Modify: `data/ib_realtime.py:779`
- Test: `tests/test_ib_realtime.py` (update 4 existing tests)

**Interfaces:**
- Consumes: `run_gap_fill_with_retries` from Task 2 (already in the same module, no import needed).
- Produces: no change to `start()`'s public signature.

- [ ] **Step 1: Update the existing tests that will break**

Four tests in `tests/test_ib_realtime.py` patch `src.gap_fill` directly so `start()`'s prologue is a no-op. Once `start()` calls `run_gap_fill_with_retries(self.gap_fill, self._bar_data_dir)` instead of `self.gap_fill()` directly, the real `run_gap_fill_with_retries` would run against `tmp_path` (no parquet files → gap `inf` → infinite retry loop with `time.sleep(650)`), hanging these tests. Patch `data.ib_realtime.run_gap_fill_with_retries` in each so it immediately invokes the passed-in callable once (preserving each test's original intent) instead of looping.

In `test_gap_fill_not_called_from_start` (lines 213-235), change the `with` block from:

```python
    with patch("ib_insync.IB", return_value=ib_mock), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill") as mock_public_gap_fill, \
         patch.object(src, "_gap_fill") as mock_gap_fill, \
         patch.object(src, "_setup_subscriptions"):
```

to:

```python
    with patch("ib_insync.IB", return_value=ib_mock), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill") as mock_public_gap_fill, \
         patch.object(src, "_gap_fill") as mock_gap_fill, \
         patch("data.ib_realtime.run_gap_fill_with_retries",
               side_effect=lambda fn, *_a, **_kw: fn()), \
         patch.object(src, "_setup_subscriptions"):
```

(rest of the test body unchanged).

In `test_gateway_disconnect_raises_ibgateway_disconnected_error` (lines 238-275), change:

```python
    with patch("ib_insync.IB", return_value=fake_ib), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill"), \
         patch.object(src, "_setup_subscriptions"):
```

to:

```python
    with patch("ib_insync.IB", return_value=fake_ib), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill"), \
         patch("data.ib_realtime.run_gap_fill_with_retries",
               side_effect=lambda fn, *_a, **_kw: fn()), \
         patch.object(src, "_setup_subscriptions"):
```

In `test_ibgateway_disconnected_error_not_retried` (lines 304-341), change:

```python
    with patch("ib_insync.IB", return_value=FakeIB()), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill"), \
         patch.object(src, "_setup_subscriptions"):
```

to:

```python
    with patch("ib_insync.IB", return_value=FakeIB()), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill"), \
         patch("data.ib_realtime.run_gap_fill_with_retries",
               side_effect=lambda fn, *_a, **_kw: fn()), \
         patch.object(src, "_setup_subscriptions"):
```

In `test_1s_dfs_freed_after_gap_fill_in_start` (lines 666-701), change:

```python
    with patch("ib_insync.IB", return_value=FakeIB()), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill"), \
         patch.object(src, "_setup_subscriptions"):
```

to:

```python
    with patch("ib_insync.IB", return_value=FakeIB()), \
         patch("ib_insync.Future"), \
         patch("ib_insync.util") as util_mock, \
         patch.object(src, "gap_fill"), \
         patch("data.ib_realtime.run_gap_fill_with_retries",
               side_effect=lambda fn, *_a, **_kw: fn()), \
         patch.object(src, "_setup_subscriptions"):
```

- [ ] **Step 2: Run tests to verify they still pass against the current (unmodified) start())**

Run: `uv run pytest tests/test_ib_realtime.py -v -k "gap_fill_not_called_from_start or gateway_disconnect_raises_ibgateway_disconnected_error or ibgateway_disconnected_error_not_retried or 1s_dfs_freed_after_gap_fill_in_start"`
Expected: All PASS — patching an unused name (`run_gap_fill_with_retries` isn't called by `start()` yet) is harmless; this step just confirms the edits above don't break anything before Step 3 changes `start()` itself.

- [ ] **Step 3: Wire `start()`**

In `data/ib_realtime.py`, replace line 779:

```python
        self.gap_fill()
```

with:

```python
        run_gap_fill_with_retries(self.gap_fill, self._bar_data_dir)
```

(This is inside `def start(self)`, which already has `from ib_insync import IB, Future, util` on the line above per the existing code at `data/ib_realtime.py:773-779`; `run_gap_fill_with_retries` is a module-level function in the same file from Task 2, no import needed.)

- [ ] **Step 4: Run the full test_ib_realtime.py suite to verify no regressions**

Run: `uv run pytest tests/test_ib_realtime.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add data/ib_realtime.py tests/test_ib_realtime.py
git commit -m "feat(gap-fill): route production start() through run_gap_fill_with_retries"
```

---

### Task 5: `automation/main.py` — catch `GapFillFailedError`, exit 11

**Files:**
- Modify: `automation/main.py:40` (import), `automation/main.py:1160-1185` (except clause)
- Test: `tests/test_automation_main.py` (append new test, mirroring `_setup_ib_disconnect` pattern at lines 729-778)

**Interfaces:**
- Consumes: `GapFillFailedError` from Task 1 (`data.ib_realtime`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_automation_main.py`, after `test_ib_disconnect_already_flat_exits_immediately` (which follows the `_setup_ib_disconnect` helper at line 729):

```python
def test_gap_fill_failed_exits_11(monkeypatch, tmp_path):
    """GapFillFailedError from _ib_source.start() prints a loud message and exits 11."""
    from data.ib_realtime import GapFillFailedError
    import automation.main as am

    monkeypatch.setenv("PMT_WEBHOOK_URL", "https://example.com")
    monkeypatch.setenv("PMT_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(am, "POSITION_FILE", tmp_path / "data" / "live_position.json")

    mock_executor = MagicMock()
    mock_ib = MagicMock()
    mock_ib.start.side_effect = GapFillFailedError(
        {"MES_1s.parquet": 200.0}, RuntimeError("ib broke")
    )

    with patch("automation.main.PickMyTradeExecutor", return_value=mock_executor), \
         patch("automation.main.IbRealtimeSource", return_value=mock_ib), \
         patch("automation.main.HypothesisManager"), \
         patch("automation.main._load_hist_mnq", return_value=pd.DataFrame()):
        with pytest.raises(SystemExit) as exc_info:
            am.main()

    assert exc_info.value.code == 11
```

(This mirrors `test_ib_disconnect_already_flat_exits_immediately`'s structure without needing the `_setup_ib_disconnect` helper, since `GapFillFailedError` handling doesn't need the position-file/with_v1_position setup that `IbGatewayDisconnectedError` handling does.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_automation_main.py -v -k test_gap_fill_failed_exits_11`
Expected: FAIL — either `GapFillFailedError` propagates uncaught out of `am.main()` (no matching `except` clause) or the exit code isn't 11.

- [ ] **Step 3: Add the import and except clause**

In `automation/main.py:40`, change:

```python
from data.ib_realtime import IbGatewayDisconnectedError, IbRealtimeSource
```

to:

```python
from data.ib_realtime import GapFillFailedError, IbGatewayDisconnectedError, IbRealtimeSource
```

In `automation/main.py`, the current block at lines 1159-1186 reads:

```python
    try:
        _ib_source.start()  # blocks; retry loop is inside IbRealtimeSource
    except IbGatewayDisconnectedError:
        import time as _wall_clock
        import live_orders as _lo_dc
        _has_position = (
            (_state == "MANAGING" and _position is not None)
            or _lo_dc.has_active_position()
        )
        if _has_position:
            print(
                "[automation] IB disconnect with open position — "
                "30s grace period (reconnect IB now to resume)",
                flush=True,
            )
            _wall_clock.sleep(30)
            print("[automation] Grace period expired — issuing hard close via PMT", flush=True)
            try:
                _executor.place_close("ib-disconnect")
            except Exception as _dc_exc:
                print(
                    f"[automation] Hard close failed (leaving for AutoLiq): {_dc_exc}",
                    flush=True,
                )
        # Gateway shut down — executor.stop() runs in finally; exit code 2 signals orchestrator
        print("[automation] IB Gateway disconnected — exiting with code 2", flush=True)
        sys.exit(2)
    finally:
```

Insert a new `except GapFillFailedError as exc:` clause between the `except IbGatewayDisconnectedError:` block and `finally:`:

```python
    try:
        _ib_source.start()  # blocks; retry loop is inside IbRealtimeSource
    except IbGatewayDisconnectedError:
        import time as _wall_clock
        import live_orders as _lo_dc
        _has_position = (
            (_state == "MANAGING" and _position is not None)
            or _lo_dc.has_active_position()
        )
        if _has_position:
            print(
                "[automation] IB disconnect with open position — "
                "30s grace period (reconnect IB now to resume)",
                flush=True,
            )
            _wall_clock.sleep(30)
            print("[automation] Grace period expired — issuing hard close via PMT", flush=True)
            try:
                _executor.place_close("ib-disconnect")
            except Exception as _dc_exc:
                print(
                    f"[automation] Hard close failed (leaving for AutoLiq): {_dc_exc}",
                    flush=True,
                )
        # Gateway shut down — executor.stop() runs in finally; exit code 2 signals orchestrator
        print("[automation] IB Gateway disconnected — exiting with code 2", flush=True)
        sys.exit(2)
    except GapFillFailedError as exc:
        print(
            "[automation] *** Gap-fill failed 5 consecutive rounds — "
            f"last error: {exc.last_error}; gaps (hours): {exc.gaps_hours}. "
            "Run 'python trade.py gap-fill' manually, then restart. "
            "Exiting with code 11. ***",
            flush=True,
        )
        sys.exit(11)
    finally:
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_automation_main.py -v -k test_gap_fill_failed_exits_11`
Expected: PASS.

- [ ] **Step 5: Run the full test_automation_main.py suite to check for regressions**

Run: `uv run pytest tests/test_automation_main.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add automation/main.py tests/test_automation_main.py
git commit -m "feat(gap-fill): exit 11 on GapFillFailedError in automation.main"
```

---

### Task 6: `orchestrator/main.py` — `_GracefulStop(exit_code)`, health-check special-case

**Files:**
- Modify: `orchestrator/main.py:241-251` (`_GracefulStop` class), `orchestrator/main.py:325-350` (`_make_ib_health_check`), `orchestrator/main.py:577-580` (`except _GracefulStop:` handler), `orchestrator/main.py` top imports
- Test: `tests/test_orchestrator_main.py:258-286` (add new tests, update regression assertion)

**Interfaces:**
- Consumes: `GapFillFailedError` from Task 1 (`data.ib_realtime`).
- Produces: `_GracefulStop(exit_code: int = 0)` — existing callers (`_check_stop_requested()` at line 251, which raises bare `_GracefulStop()`) are unaffected since `exit_code` defaults to `0`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_orchestrator_main.py`, add two new tests after `test_make_ib_health_check_noop_when_thread_alive` (currently ending at line 286):

```python
def test_make_ib_health_check_gap_fill_failed_exits_11(capsys):
    """A GapFillFailedError from the pre-session thread prints a specific message and
    raises _GracefulStop with exit_code=11 (not the generic maintenance-break 0)."""
    from data.ib_realtime import GapFillFailedError
    from orchestrator.main import _make_ib_health_check, _GracefulStop

    thread = MagicMock()
    thread.is_alive.return_value = False
    exc = GapFillFailedError({"MES_1s.parquet": 200.0}, RuntimeError("ib broke"))
    check = _make_ib_health_check(thread, [exc])

    with pytest.raises(_GracefulStop) as exc_info:
        check()

    assert exc_info.value.exit_code == 11
    out = capsys.readouterr().out
    assert "trade.py gap-fill" in out
    assert "maintenance break" not in out.lower()


def test_graceful_stop_defaults_to_exit_code_0():
    from orchestrator.main import _GracefulStop
    assert _GracefulStop().exit_code == 0
```

Update the existing `test_make_ib_health_check_raises_graceful_stop_on_thread_death` (lines 262-278) to also assert the exit code stays 0 for the routine case — change:

```python
    check = _make_ib_health_check(thread, [RuntimeError("IB Gateway closed the connection")])
    with pytest.raises(_GracefulStop):
        check()
    out = capsys.readouterr().out
    assert "maintenance break" in out.lower()
    assert "CRITICAL" not in out and "Terminating now" not in out
```

to:

```python
    check = _make_ib_health_check(thread, [RuntimeError("IB Gateway closed the connection")])
    with pytest.raises(_GracefulStop) as exc_info:
        check()
    assert exc_info.value.exit_code == 0
    out = capsys.readouterr().out
    assert "maintenance break" in out.lower()
    assert "CRITICAL" not in out and "Terminating now" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator_main.py -v -k "gap_fill_failed_exits_11 or graceful_stop_defaults_to_exit_code_0 or raises_graceful_stop_on_thread_death"`
Expected: FAIL — `_GracefulStop` has no `exit_code` attribute yet; `_make_ib_health_check` doesn't special-case `GapFillFailedError`.

- [ ] **Step 3: Add the import**

In `orchestrator/main.py`, near the existing `from gap_fill import check_ib_reachable as _check_ib_reachable` (line 77), add:

```python
from gap_fill import check_ib_reachable as _check_ib_reachable
from data.ib_realtime import GapFillFailedError
```

- [ ] **Step 4: Give `_GracefulStop` an `exit_code` attribute**

Replace `orchestrator/main.py:241-242`:

```python
class _GracefulStop(Exception):
    """Raised when trade.py terminate writes the stop-request sentinel file."""
```

with:

```python
class _GracefulStop(Exception):
    """Raised when trade.py terminate writes the stop-request sentinel file, or when a
    background thread signals a condition that should cleanly end this orchestrator run."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        super().__init__()
```

- [ ] **Step 5: Special-case `GapFillFailedError` in `_make_ib_health_check`**

Replace the `check()` function body inside `_make_ib_health_check` (`orchestrator/main.py:341-349`):

```python
    def check() -> None:
        if not thread.is_alive() and thread_exc[0] is not None:
            print(
                f"[ORCH] Pre-session IB connection ended ({thread_exc[0]}) — expected during the "
                "17:00-18:00 ET maintenance break. Shutting down cleanly; relaunch for the "
                "next session.",
                flush=True,
            )
            raise _GracefulStop()
    return check
```

with:

```python
    def check() -> None:
        if not thread.is_alive() and thread_exc[0] is not None:
            exc = thread_exc[0]
            if isinstance(exc, GapFillFailedError):
                print(
                    "[ORCH] *** Gap-fill failed 5 consecutive rounds — "
                    f"last error: {exc.last_error}; gaps (hours): {exc.gaps_hours}. "
                    "Run 'python trade.py gap-fill' manually, then relaunch the orchestrator. ***",
                    flush=True,
                )
                raise _GracefulStop(exit_code=11)
            print(
                f"[ORCH] Pre-session IB connection ended ({exc}) — expected during the "
                "17:00-18:00 ET maintenance break. Shutting down cleanly; relaunch for the "
                "next session.",
                flush=True,
            )
            raise _GracefulStop()
    return check
```

- [ ] **Step 6: Use the exit code in the `except _GracefulStop:` handler**

Replace `orchestrator/main.py:577-580`:

```python
    except _GracefulStop:
        print("\n[ORCH] Stop requested — shutting down gracefully.", flush=True)
        _stop_pre_session_ib(_pre_src, _pre_thr)
        sys.exit(0)
```

with:

```python
    except _GracefulStop as _stop:
        print("\n[ORCH] Stop requested — shutting down gracefully.", flush=True)
        _stop_pre_session_ib(_pre_src, _pre_thr)
        sys.exit(_stop.exit_code)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_main.py -v -k "gap_fill_failed_exits_11 or graceful_stop_defaults_to_exit_code_0 or raises_graceful_stop_on_thread_death"`
Expected: PASS.

- [ ] **Step 8: Run the full test_orchestrator_main.py suite to check for regressions**

Run: `uv run pytest tests/test_orchestrator_main.py -v`
Expected: All PASS.

- [ ] **Step 9: Commit**

```bash
git add orchestrator/main.py tests/test_orchestrator_main.py
git commit -m "feat(gap-fill): exit 11 on GapFillFailedError in orchestrator.main"
```

---

### Task 7: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: All PASS. No test outside the files touched in Tasks 1-6 should be affected — `run_gap_fill_with_retries` and `GapFillFailedError` are new names, `gap_fill_until_now()`'s and `start()`'s public signatures are unchanged, and `_GracefulStop`'s `exit_code` parameter defaults to `0` so the one other raise site (`_check_stop_requested()` at `orchestrator/main.py:251`, unmodified) behaves exactly as before.

- [ ] **Step 2: Manually sanity-check the offline path against a live IB Gateway (optional but recommended before relying on this in production)**

Run: `uv run python trade.py gap-fill` with IB Gateway connected and a real gap present (or an already-caught-up state, which should return immediately with `[gap_fill] Caught up` and no `time.sleep` calls). This is the same command used throughout the 2026-07-10 session that motivated this change — it should now need only one invocation regardless of gap size, rather than requiring manual re-runs every ~10 minutes.
