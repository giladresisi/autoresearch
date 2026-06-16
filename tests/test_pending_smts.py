# tests/test_pending_smts.py
# Unit tests for GIL-25 Phase 1.2: cross-session carry of non-invalidated SMTs.
#
# Covers:
#   Task 1 — the pure re-validation helper smt_detect.pending_smt_terminal.
#   Task 3 — the pure ingest/revalidate-and-filter core (revalidate_and_filter_pending).
#   Task 4 — dedup (logical-key + price-proximity + carried-vs-carried newest).
#   Task 5 — age-cap (business-day count, Fri->Mon = 1).
#   Task 6 — session_pipeline cold-start ingest end-to-end (_ingest_pending_smts).

from __future__ import annotations

import pandas as pd
import pytest

import smt_detect
from smt_detect import (
    FULFILL_PTS_MNQ,
    INVALIDATE_PTS_MNQ,
    pending_smt_terminal,
)

_ET = "America/New_York"


# ===========================================================================
# Task 1 — pure re-validation helper
# ===========================================================================
class TestPendingTerminal:
    def test_pending_terminal_long_fulfilled(self):
        # long, day tier: FULFILL mnq = 40. fire=21000 → fulfilled if window_high >= 21040.
        out = pending_smt_terminal("long", 21000.0, "day",
                                   window_high=21041.0, window_low=20999.0)
        assert out == "fulfilled"

    def test_pending_terminal_long_invalidated(self):
        # long, day tier: INVALIDATE mnq = 40. fire=21000 → invalidated if window_low <= 20960.
        out = pending_smt_terminal("long", 21000.0, "day",
                                   window_high=21010.0, window_low=20959.0)
        assert out == "invalidated"

    def test_pending_terminal_short_fulfilled(self):
        # short, day tier: fulfilled if window_low <= fire - FULFILL (21000-40=20960).
        out = pending_smt_terminal("short", 21000.0, "day",
                                   window_high=21010.0, window_low=20959.0)
        assert out == "fulfilled"

    def test_pending_terminal_short_invalidated(self):
        # short, day tier: invalidated if window_high >= fire + INVALIDATE (21000+40=21040).
        out = pending_smt_terminal("short", 21000.0, "day",
                                   window_high=21041.0, window_low=20999.0)
        assert out == "invalidated"

    def test_pending_terminal_unfulfilled(self):
        # Within both thresholds either way → unfulfilled.
        out = pending_smt_terminal("long", 21000.0, "day",
                                   window_high=21020.0, window_low=20980.0)
        assert out == "unfulfilled"

    def test_pending_terminal_fulfilled_precedence(self):
        # Window crosses BOTH (huge range) → fulfilled wins over invalidated.
        out = pending_smt_terminal("long", 21000.0, "day",
                                   window_high=21100.0, window_low=20900.0)
        assert out == "fulfilled"

    def test_pending_terminal_unknown_tier_falls_back(self):
        # Unknown tier → falls back to the "session" row, no raise.
        sess_fulfill = FULFILL_PTS_MNQ["session"]
        out = pending_smt_terminal("long", 21000.0, "bogus",
                                   window_high=21000.0 + sess_fulfill + 1, window_low=20999.0)
        assert out == "fulfilled"
        # And a within-threshold window with the bogus tier stays unfulfilled.
        out2 = pending_smt_terminal("long", 21000.0, "bogus",
                                    window_high=21000.0, window_low=20999.0)
        assert out2 == "unfulfilled"


# ===========================================================================
# Helpers for Tasks 3-6
# ===========================================================================
def _entry(price, fire_price, direction, tier="day", ref_name="prev1_day_low",
           type_="wick", fire_time="2026-06-08T15:14:00-04:00",
           side="bullish", leader="mnq", mes_price=3000.0,
           session_date="2026-06-08", valid=True):
    return {
        "price": float(price),
        "fire_price": float(fire_price),
        "direction": direction,
        "tier": tier,
        "type": type_,
        "fire_time": fire_time,
        "ref_name": ref_name,
        "side": side,
        "leader": leader,
        "mes_price": float(mes_price),
        "session_date": session_date,
        "valid": valid,
    }


def _hist_1m(rows):
    """rows: list of (ts_iso, high, low). Build a 1m hist DataFrame indexed by tz-aware ts."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    return pd.DataFrame(
        {"High": [r[1] for r in rows], "Low": [r[2] for r in rows],
         "Close": [(r[1] + r[2]) / 2 for r in rows]},
        index=idx,
    )


# ===========================================================================
# Task 3 — ingest keeps unfulfilled / drops terminal
# ===========================================================================
class TestRevalidateAndFilter:
    def _session_open(self):
        return pd.Timestamp("2026-06-08 18:00", tz=_ET)

    def _today(self):
        return pd.Timestamp("2026-06-09").date()

    def test_ingest_keeps_unfulfilled(self):
        # long fire=21000 day; window stays within thresholds → survives.
        e = _entry(price=21010.0, fire_price=21000.0, direction="long")
        hist = _hist_1m([
            ("2026-06-08T16:00:00-04:00", 21020.0, 20990.0),
            ("2026-06-08T20:00:00-04:00", 21015.0, 20985.0),
        ])
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [e], hist, self._session_open(), self._today(), existing_active=[],
        )
        assert len(survivors) == 1
        rec = survivors[0]
        assert rec["kind"] == "smt"
        assert rec["ref_name"] == "prev1_day_low"
        assert rec["direction"] == "long"
        assert rec["tier"] == "day"
        assert rec["time"] == e["fire_time"]
        assert rec["mnq_lvl_price"] == 21010.0
        assert rec["fulfilled"] is False and rec["invalidated"] is False
        assert any(a["reason"] == "ingested" for a in audit)

    def test_ingest_drops_fulfilled(self):
        e = _entry(price=21010.0, fire_price=21000.0, direction="long")
        # window high runs > fire+FULFILL(40) → fulfilled overnight → dropped. The bar must
        # sit in (fire_time 15:14, session_open 18:00).
        hist = _hist_1m([("2026-06-08T16:00:00-04:00", 21055.0, 20990.0)])
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [e], hist, self._session_open(), self._today(), existing_active=[],
        )
        assert survivors == []
        assert any(a["reason"] == "drop_fulfilled" for a in audit)

    def test_ingest_drops_invalidated(self):
        e = _entry(price=21010.0, fire_price=21000.0, direction="long")
        # window low runs < fire-INVALIDATE(40) → invalidated → dropped. Bar in-window.
        hist = _hist_1m([("2026-06-08T16:00:00-04:00", 21005.0, 20950.0)])
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [e], hist, self._session_open(), self._today(), existing_active=[],
        )
        assert survivors == []
        assert any(a["reason"] == "drop_invalidated" for a in audit)

    def test_ingest_empty_window_keeps(self):
        # No bars in (fire_time, session_open) → kept (no evidence of take-out).
        e = _entry(price=21010.0, fire_price=21000.0, direction="long")
        hist = _hist_1m([("2026-06-05T10:00:00-04:00", 99999.0, 0.0)])  # all before fire
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [e], hist, self._session_open(), self._today(), existing_active=[],
        )
        assert len(survivors) == 1
        assert any(a["reason"] == "ingested" for a in audit)


# ===========================================================================
# Task 4 — dedup
# ===========================================================================
class TestDedup:
    def _session_open(self):
        return pd.Timestamp("2026-06-08 18:00", tz=_ET)

    def _today(self):
        return pd.Timestamp("2026-06-09").date()

    def _keep_window(self):
        return _hist_1m([("2026-06-08T20:00:00-04:00", 21015.0, 20990.0)])

    def test_dedup_logical_key(self):
        # carried (ref_name, direction) already present in today's fresh active set → dropped.
        e = _entry(price=21010.0, fire_price=21000.0, direction="long",
                   ref_name="prev1_day_low")
        fresh = [{"kind": "smt", "ref_name": "prev1_day_low", "direction": "long",
                  "mnq_lvl_price": 30000.0}]
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [e], self._keep_window(), self._session_open(), self._today(),
            existing_active=fresh,
        )
        assert survivors == []
        assert any(a["reason"] == "drop_dedup" for a in audit)

    def test_dedup_price_proximity(self):
        # carried price within DEDUP_TOL_PTS of a fresh member, same direction,
        # DIFFERENT ref_name → dropped.
        e = _entry(price=21010.0, fire_price=21000.0, direction="long",
                   ref_name="prev1_day_low")
        fresh = [{"kind": "smt", "ref_name": "week_low", "direction": "long",
                  "mnq_lvl_price": 21012.0}]  # within 5pt tol
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [e], self._keep_window(), self._session_open(), self._today(),
            existing_active=fresh,
        )
        assert survivors == []
        assert any(a["reason"] == "drop_dedup" for a in audit)

    def test_dedup_carried_vs_carried_keeps_newest(self):
        older = _entry(price=21010.0, fire_price=21000.0, direction="long",
                       ref_name="prev1_day_low",
                       fire_time="2026-06-08T10:00:00-04:00")
        newer = _entry(price=21010.0, fire_price=21000.0, direction="long",
                       ref_name="prev1_day_low",
                       fire_time="2026-06-08T15:14:00-04:00")
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [older, newer], self._keep_window(), self._session_open(), self._today(),
            existing_active=[],
        )
        assert len(survivors) == 1
        assert survivors[0]["time"] == "2026-06-08T15:14:00-04:00"


# ===========================================================================
# Task 5 — age-cap (business-day count)
# ===========================================================================
class TestAgeCap:
    def _session_open(self):
        return pd.Timestamp("2026-06-08 18:00", tz=_ET)

    def _keep_window(self):
        return _hist_1m([("2026-06-08T20:00:00-04:00", 21015.0, 20990.0)])

    def test_age_cap_drops_old(self):
        # entry from 2026-06-04 (Thu); today=2026-06-09 (Tue) → 3 business days > K=1 → drop.
        e = _entry(price=21010.0, fire_price=21000.0, direction="long",
                   session_date="2026-06-04",
                   fire_time="2026-06-04T15:14:00-04:00")
        today = pd.Timestamp("2026-06-09").date()
        survivors, audit = smt_detect.revalidate_and_filter_pending(
            [e], self._keep_window(), self._session_open(), today, existing_active=[],
        )
        assert survivors == []
        assert any(a["reason"] == "drop_age" for a in audit)

    def test_age_cap_keeps_prior_session(self):
        # entry 2026-06-08 (Mon); today 2026-06-09 (Tue) → 1 business day = K → keep.
        e = _entry(price=21010.0, fire_price=21000.0, direction="long",
                   session_date="2026-06-08",
                   fire_time="2026-06-08T15:14:00-04:00")
        today = pd.Timestamp("2026-06-09").date()
        survivors, _ = smt_detect.revalidate_and_filter_pending(
            [e], self._keep_window(), self._session_open(), today, existing_active=[],
        )
        assert len(survivors) == 1

    def test_age_cap_friday_to_monday(self):
        # Fri 2026-06-05 entry; today Mon 2026-06-08 → 1 business day → kept (locks bdays).
        e = _entry(price=21010.0, fire_price=21000.0, direction="long",
                   session_date="2026-06-05",
                   fire_time="2026-06-05T15:14:00-04:00")
        session_open = pd.Timestamp("2026-06-05 18:00", tz=_ET)
        keep_window = _hist_1m([("2026-06-05T20:00:00-04:00", 21015.0, 20990.0)])
        today = pd.Timestamp("2026-06-08").date()
        survivors, _ = smt_detect.revalidate_and_filter_pending(
            [e], keep_window, session_open, today, existing_active=[],
        )
        assert len(survivors) == 1


# ===========================================================================
# Task 6 — session_pipeline cold-start ingest (driven via __new__, in-memory state)
# ===========================================================================
import smt_state
from session_pipeline import SessionPipeline


@pytest.fixture()
def _inmem(monkeypatch, tmp_path):
    import paths
    monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)
    smt_state.set_in_memory_mode(True)
    try:
        yield
    finally:
        smt_state.set_in_memory_mode(False)


def _make_pipe(hist_mnq, events):
    """Build a SessionPipeline without running __init__ (no daily compute / parquet IO)."""
    pipe = SessionPipeline.__new__(SessionPipeline)
    pipe._hist_mnq_1m = hist_mnq
    pipe._hist_mes_1m = None
    pipe._emit = events.append
    pipe._detect_state = {}
    return pipe


class TestColdStartIngest:
    def test_written_pending_ingested_on_cold_start(self, _inmem):
        # Pre-write a still-valid carried entry (long, day, fire 21000). The June-9 ingest
        # `now` = inside the June-9 session (session_open = June-8 18:00 ET). Overnight window
        # bars stay within thresholds → survives.
        entry = _entry(price=21010.0, fire_price=21000.0, direction="long",
                       ref_name="prev1_day_low", session_date="2026-06-08",
                       fire_time="2026-06-08T15:14:00-04:00")
        smt_state.save_pending_smts({"entries": [entry], "schema": 1})
        # Seed a benign empty active set into hypothesis (merge target).
        hyp = smt_state.load_hypothesis()
        hyp["smt_active_set"] = []
        smt_state.save_hypothesis(hyp)

        hist = _hist_1m([
            ("2026-06-08T16:00:00-04:00", 21020.0, 20990.0),  # in-window, within thresholds
        ])
        events: list = []
        pipe = _make_pipe(hist, events)
        now = pd.Timestamp("2026-06-08 19:30", tz=_ET)  # June-9 session, just after open

        pipe._ingest_pending_smts(now, pd.DataFrame())

        active = smt_state.load_hypothesis().get("smt_active_set", [])
        carried = [r for r in active if r.get("carried")]
        assert len(carried) == 1
        assert carried[0]["ref_name"] == "prev1_day_low"
        assert carried[0]["direction"] == "long"
        # An ingested smt-carry event was emitted.
        carry_ev = [e for e in events if e.get("kind") == "smt-carry"]
        assert any(e.get("reason") == "ingested" and e.get("source") == "v2-carry"
                   for e in carry_ev)

    def test_drops_when_taken_out_overnight(self, _inmem):
        # Same entry but the overnight window runs FULFILL beyond → dropped, none seeded.
        entry = _entry(price=21010.0, fire_price=21000.0, direction="long",
                       ref_name="prev1_day_low", session_date="2026-06-08",
                       fire_time="2026-06-08T15:14:00-04:00")
        smt_state.save_pending_smts({"entries": [entry], "schema": 1})
        smt_state.save_hypothesis({**smt_state.DEFAULT_HYPOTHESIS, "smt_active_set": []})
        hist = _hist_1m([("2026-06-08T16:00:00-04:00", 21060.0, 20990.0)])  # fulfilled
        events: list = []
        pipe = _make_pipe(hist, events)
        now = pd.Timestamp("2026-06-08 19:30", tz=_ET)
        pipe._ingest_pending_smts(now, pd.DataFrame())
        active = smt_state.load_hypothesis().get("smt_active_set", [])
        assert [r for r in active if r.get("carried")] == []
        assert any(e.get("reason") == "drop_fulfilled"
                   for e in events if e.get("kind") == "smt-carry")

    def test_empty_pending_is_noop(self, _inmem):
        smt_state.save_pending_smts({"entries": [], "schema": 1})
        smt_state.save_hypothesis({**smt_state.DEFAULT_HYPOTHESIS, "smt_active_set": []})
        events: list = []
        pipe = _make_pipe(_hist_1m([("2026-06-08T16:00:00-04:00", 21020.0, 20990.0)]), events)
        now = pd.Timestamp("2026-06-08 19:30", tz=_ET)
        pipe._ingest_pending_smts(now, pd.DataFrame())
        assert [e for e in events if e.get("kind") == "smt-carry"] == []


class TestActiveRecordToPending:
    def test_maps_fire_price_from_detect_state(self, _inmem):
        events: list = []
        pipe = _make_pipe(None, events)
        pipe._detect_state = {
            "prev1_day_low|long|wick": {"fire_mnq_close": 20987.5},
        }
        rec = {
            "kind": "smt", "ref_name": "prev1_day_low", "direction": "long",
            "type": "wick", "tier": "day", "side": "bullish", "leader": "mnq",
            "mnq_lvl_price": 21010.0, "mnq_price": 21008.0, "mes_price": 3000.0,
            "time": "2026-06-08T15:14:00-04:00",
            "key": "prev1_day_low|long|wick", "keys": ["prev1_day_low|long|wick"],
        }
        out = pipe._active_record_to_pending(rec, "2026-06-08")
        assert out["price"] == 21010.0
        assert out["fire_price"] == 20987.5   # from detect_state
        assert out["direction"] == "long"
        assert out["tier"] == "day"
        assert out["fire_time"] == "2026-06-08T15:14:00-04:00"
        assert out["session_date"] == "2026-06-08"
        assert out["valid"] is True

    def test_fire_price_falls_back_to_mnq_price(self, _inmem):
        events: list = []
        pipe = _make_pipe(None, events)
        pipe._detect_state = {}  # no fire_mnq_close
        rec = {
            "kind": "smt", "ref_name": "week_low", "direction": "long", "type": "wick",
            "tier": "week", "mnq_lvl_price": 21010.0, "mnq_price": 21008.0,
            "time": "2026-06-08T15:14:00-04:00",
            "key": "week_low|long|wick", "keys": ["week_low|long|wick"],
        }
        out = pipe._active_record_to_pending(rec, "2026-06-08")
        assert out["fire_price"] == 21008.0   # fallback to mnq_price


class TestColdStartGating:
    """Verify _ingest_pending_smts is wired INSIDE the cold-start block of on_session_start
    (cold start → called; warm restart → skipped). Heavy daily/warm-up steps are stubbed so
    the test is hermetic and does not need live parquets."""

    def _patch_pipeline(self, monkeypatch, calls):
        # Stub the expensive/IO steps so on_session_start exercises only the gating logic.
        import hypothesis as _hyp_mod
        monkeypatch.setattr(SessionPipeline, "on_daily_or_startup",
                            lambda self, now, df: None)
        monkeypatch.setattr(SessionPipeline, "_seed_pre_session_invalidation",
                            lambda self, now: None)
        monkeypatch.setattr(SessionPipeline, "_warmup_replay_smts",
                            lambda self, now, df: [])
        monkeypatch.setattr(SessionPipeline, "_ingest_pending_smts",
                            lambda self, now, df: calls.append(now))
        monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **k: [])

    def test_ingest_called_on_cold_start(self, _inmem, monkeypatch):
        calls: list = []
        self._patch_pipeline(monkeypatch, calls)
        # Cold start: smts.json empty → detect_state empty (in-memory store fresh).
        events: list = []
        pipe = SessionPipeline.__new__(SessionPipeline)
        pipe._hist_mnq_1m = None
        pipe._hist_mes_1m = None
        pipe._hist_1hr = None
        pipe._hist_4hr = None
        pipe._emit = events.append
        pipe._last_hyp_cautious = ("", "")
        pipe._hyp_formation_price = None
        now = pd.Timestamp("2026-06-08 19:30", tz=_ET)
        pipe.on_session_start(now, pd.DataFrame(), force_reset=True)
        assert calls, "ingest must run on a cold start"

    def test_ingest_skipped_on_warm_restart(self, _inmem, monkeypatch):
        calls: list = []
        self._patch_pipeline(monkeypatch, calls)
        # Warm restart: pre-populate detect_state with a non-bookkeeping key in the store.
        smt_state.save_smts({"detect_state": {"prev1_day_low|long|wick": {"armed": True}},
                             "watch": {"retained": []}})
        events: list = []
        pipe = SessionPipeline.__new__(SessionPipeline)
        pipe._hist_mnq_1m = None
        pipe._hist_mes_1m = None
        pipe._hist_1hr = None
        pipe._hist_4hr = None
        pipe._emit = events.append
        pipe._last_hyp_cautious = ("", "")
        pipe._hyp_formation_price = None
        now = pd.Timestamp("2026-06-08 19:30", tz=_ET)
        pipe.on_session_start(now, pd.DataFrame(), force_reset=True)
        assert calls == [], "ingest must be skipped on a warm restart (no double-seed)"


class TestColdStartIngestEndToEnd:
    """End-to-end through on_session_start(force_reset=True) WITHOUT stubbing the ingest, so
    the carried survivor must actually land in hypothesis.json['smt_active_set'] after the
    force_reset save. Regression guard for the bug where the force_reset reset-to-default
    wiped the seed before run_hypothesis read it."""

    def _patch_heavy(self, monkeypatch):
        import hypothesis as _hyp_mod
        monkeypatch.setattr(SessionPipeline, "on_daily_or_startup",
                            lambda self, now, df: None)
        monkeypatch.setattr(SessionPipeline, "_seed_pre_session_invalidation",
                            lambda self, now: None)
        monkeypatch.setattr(SessionPipeline, "_warmup_replay_smts",
                            lambda self, now, df: [])
        monkeypatch.setattr(_hyp_mod, "run_hypothesis", lambda *a, **k: [])

    def _build(self, hist, events):
        pipe = SessionPipeline.__new__(SessionPipeline)
        pipe._hist_mnq_1m = hist
        pipe._hist_mes_1m = None
        pipe._hist_1hr = None
        pipe._hist_4hr = None
        pipe._emit = events.append
        pipe._last_hyp_cautious = ("", "")
        pipe._hyp_formation_price = None
        return pipe

    def test_carry_survives_force_reset(self, _inmem, monkeypatch):
        self._patch_heavy(monkeypatch)
        entry = _entry(price=21010.0, fire_price=21000.0, direction="long",
                       ref_name="prev1_day_low", session_date="2026-06-08",
                       fire_time="2026-06-08T15:14:00-04:00")
        smt_state.save_pending_smts({"entries": [entry], "schema": 1})
        hist = _hist_1m([("2026-06-08T16:00:00-04:00", 21020.0, 20990.0)])  # within thresholds
        events: list = []
        pipe = self._build(hist, events)
        now = pd.Timestamp("2026-06-08 19:30", tz=_ET)

        pipe.on_session_start(now, pd.DataFrame(), force_reset=True)

        active = smt_state.load_hypothesis().get("smt_active_set", [])
        carried = [r for r in active if r.get("carried")]
        assert len(carried) == 1, \
            "carried SMT must survive force_reset into smt_active_set (seen by the first hypothesis)"
        assert carried[0]["ref_name"] == "prev1_day_low"
        assert any(e.get("kind") == "smt-carry" and e.get("reason") == "ingested"
                   for e in events)

    def test_carry_seeds_on_no_force_reset(self, _inmem, monkeypatch):
        self._patch_heavy(monkeypatch)
        entry = _entry(price=21010.0, fire_price=21000.0, direction="long",
                       ref_name="prev1_day_low", session_date="2026-06-08",
                       fire_time="2026-06-08T15:14:00-04:00")
        smt_state.save_pending_smts({"entries": [entry], "schema": 1})
        hist = _hist_1m([("2026-06-08T16:00:00-04:00", 21020.0, 20990.0)])
        events: list = []
        pipe = self._build(hist, events)
        now = pd.Timestamp("2026-06-08 19:30", tz=_ET)

        pipe.on_session_start(now, pd.DataFrame(), force_reset=False)

        active = smt_state.load_hypothesis().get("smt_active_set", [])
        assert len([r for r in active if r.get("carried")]) == 1
