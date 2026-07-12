"""Phase 1 — bench lifecycle engine tests (plan 10).

Scripted deterministic providers + synthetic 1m bars; no facts, no network.
"""

import os
import sys

import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import BenchConfig  # noqa: E402
from lifecycle import BenchDecision, run_day, run_lifecycle  # noqa: E402

TZ = "America/New_York"


def mkbars(start: str, closes, highs=None, lows=None, freq="1min") -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp(start, tz=TZ), periods=len(closes), freq=freq)
    closes = [float(c) for c in closes]
    highs = [float(h) for h in (highs if highs is not None else closes)]
    lows = [float(l) for l in (lows if lows is not None else closes)]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes},
                        index=idx)


def thesis(bias="UP", dol_price=100.0, dol_level="prev1_day_high",
           falsified_if=None, exhausted_if=None, recall=None, confidence="MEDIUM"):
    return {
        "bias": bias, "regime": "TREND",
        "dol": {"level": dol_level, "price": dol_price} if bias in ("UP", "DOWN") else None,
        "falsified_if": falsified_if or [], "exhausted_if": exhausted_if or [],
        "confidence": confidence,
        "recall": recall or {"events": [], "max_age_min": 0}, "reasoning": "t",
    }


def decision(born, thesis_dict, gate="MEDIUM", failsafe=False, decision_id="d1",
             levels=None, daily_mid=None, sess_hi=None, sess_lo=None):
    born_ts = born if isinstance(born, pd.Timestamp) else pd.Timestamp(born, tz=TZ)
    return BenchDecision(
        decision_id=decision_id, born_ts=born_ts, thesis=thesis_dict,
        gate=gate, self_report=thesis_dict.get("confidence"), failsafe=failsafe,
        levels=levels or {}, daily_mid=daily_mid, sess_hi=sess_hi, sess_lo=sess_lo,
    )


def cfg(**kw):
    base = dict(latency_sec=90, safety_nets=("ttl",),
                ttl_minutes={"HIGH": 180, "MEDIUM": 90}, churn_cap=20)
    base.update(kw)
    return BenchConfig(**base)


SESS_END = pd.Timestamp("2026-06-25 16:59:00", tz=TZ)


# --------------------------------------------------------------------------- #
def test_completed_dol_touch_before_falsification():
    # UP thesis, ref ~50, DOL 60. Price climbs and touches 60 → completed.
    bars = mkbars("2026-06-25 09:00", closes=[50, 52, 55, 58, 61, 62],
                  highs=[51, 53, 56, 59, 61, 62], lows=[49, 51, 54, 57, 60, 61])
    d = decision("2026-06-25 09:00", thesis(bias="UP", dol_price=60.0))
    lc = run_lifecycle(d, bars, cfg(), SESS_END)
    assert lc.cause == "completed"
    # arrival at 09:01:30 → first bar 09:02 (ref = close 55). DOL 60 hit at 09:04 (high 61).
    assert lc.arrival_price == 55.0
    assert lc.died_ts == pd.Timestamp("2026-06-25 09:04", tz=TZ)
    assert lc.mfe == pytest.approx(6.0)   # 61 - 55
    assert lc.dist_to_dol_pct == 100.0


def test_falsified_predicate_fires_and_recall_scheduled():
    # UP thesis falsified when price closes below 45. Price drops through it.
    fals = [{"type": "price_beyond", "price": 45.0, "side": "below"}]
    bars = mkbars("2026-06-25 09:00", closes=[50, 52, 51, 44, 43, 40],
                  highs=[51, 53, 52, 46, 44, 41], lows=[49, 51, 50, 43, 42, 39])
    d = decision("2026-06-25 09:00", thesis(bias="UP", dol_price=70.0, falsified_if=fals))
    lc = run_lifecycle(d, bars, cfg(), SESS_END)
    assert lc.cause == "falsified"
    # ref = close 51 (09:02). Peak favorable before falsification: high 52 (09:02) → mfe 1.
    assert lc.mfe == pytest.approx(1.0)
    # A day walk re-calls L1 at the death bar.
    seen = []

    def provider(trigger):
        seen.append(trigger)
        # First call falsifies quickly; second call stands to session end.
        if len(seen) == 1:
            return decision(trigger, thesis(bias="UP", dol_price=70.0, falsified_if=fals))
        return decision(trigger, thesis(bias="UP", dol_price=70.0), decision_id="d2")

    res = run_day(bars, provider, cfg(), pd.Timestamp("2026-06-25 09:00", tz=TZ), SESS_END)
    assert seen[0] == pd.Timestamp("2026-06-25 09:00", tz=TZ)
    assert seen[1] == res.lifecycles[0].died_ts        # re-call at the death bar
    assert res.lifecycles[0].cause == "falsified"


def test_arrival_gating_pre_arrival_predicate_does_not_fire():
    # Falsify-below-45 is TRUE at 09:00/09:01 (pre-arrival) but the thesis must ignore
    # those bars; price recovers above 45 by arrival, so no falsification fires.
    fals = [{"type": "price_beyond", "price": 45.0, "side": "below"}]
    bars = mkbars("2026-06-25 09:00", closes=[40, 42, 55, 56, 57, 58],
                  highs=[41, 43, 56, 57, 58, 59], lows=[39, 41, 54, 55, 56, 57])
    d = decision("2026-06-25 09:00", thesis(bias="UP", dol_price=70.0, falsified_if=fals))
    lc = run_lifecycle(d, bars, cfg(), SESS_END)
    # arrival 09:01:30 → walk starts at 09:02 (close 55, low 54) — never below 45.
    assert lc.arrival_price == 55.0
    assert lc.cause == "session_end"


def test_low_conf_recall_event_fires():
    recall = {"events": [{"type": "price_beyond", "price": 60.0, "side": "above"}],
              "max_age_min": 0}
    bars = mkbars("2026-06-25 09:00", closes=[50, 52, 55, 61, 62, 63],
                  highs=[51, 53, 56, 62, 63, 64], lows=[49, 51, 54, 60, 61, 62])
    d = decision("2026-06-25 09:00", thesis(bias="UP", dol_price=70.0, recall=recall,
                                            confidence="LOW"), gate="LOW")
    lc = run_lifecycle(d, bars, cfg(), SESS_END)
    assert lc.stood is False
    assert lc.cause == "recall"
    assert lc.died_ts == pd.Timestamp("2026-06-25 09:03", tz=TZ)   # close 61 > 60


def test_low_conf_max_age_fires():
    recall = {"events": [], "max_age_min": 3}
    bars = mkbars("2026-06-25 09:00", closes=[50] * 8)
    d = decision("2026-06-25 09:00", thesis(bias="NEUTRAL", dol_price=None, recall=recall,
                                            confidence="LOW"), gate="LOW")
    lc = run_lifecycle(d, bars, cfg(), SESS_END)
    assert lc.cause == "recall"
    # born 09:00, max_age 3min → first bar with age>=3 is 09:03.
    assert lc.died_ts == pd.Timestamp("2026-06-25 09:03", tz=TZ)


def test_ttl_tiers_high_survives_medium_dies():
    # 120 bars of flat price; DOL never touched. MEDIUM ttl 90 → dies at +90m; HIGH 180 survives.
    bars = mkbars("2026-06-25 09:00", closes=[50] * 200,
                  highs=[50.5] * 200, lows=[49.5] * 200)
    end = pd.Timestamp("2026-06-25 11:00", tz=TZ)   # +120m window (< HIGH ttl 180)
    d_med = decision("2026-06-25 09:00", thesis(bias="UP", dol_price=999.0), gate="MEDIUM")
    lc_med = run_lifecycle(d_med, bars, cfg(), end)
    assert lc_med.cause == "ttl"
    assert lc_med.died_ts == pd.Timestamp("2026-06-25 10:30", tz=TZ)   # born +90m
    d_high = decision("2026-06-25 09:00", thesis(bias="UP", dol_price=999.0), gate="HIGH")
    lc_high = run_lifecycle(d_high, bars, cfg(), end)
    # HIGH ttl 180 not reached within the 120m window → survives to session end.
    assert lc_high.cause == "session_end"


def test_safety_net_acceptance_flip_on_and_off():
    # UP thesis, daily mid 50. Three consecutive closes below 50 → acceptance_flip.
    bars = mkbars("2026-06-25 09:00", closes=[52, 51, 49, 48, 47, 46],
                  highs=[53, 52, 50, 49, 48, 47], lows=[51, 50, 48, 47, 46, 45])
    th = thesis(bias="UP", dol_price=999.0)
    d = decision("2026-06-25 09:00", th, daily_mid=50.0)
    on = run_lifecycle(d, bars, cfg(safety_nets=("ttl", "acceptance_flip"),
                                    acceptance_flip_n=3), SESS_END)
    assert on.cause == "safety_net"
    assert on.cause_detail == "acceptance_flip"
    # Disabled → no safety-net death.
    off = run_lifecycle(d, bars, cfg(safety_nets=("ttl",)), SESS_END)
    assert off.cause == "session_end"


def test_safety_net_opposite_extreme_on_and_off():
    # UP thesis, session low at birth 48. A new low below 48 → opposite_extreme.
    bars = mkbars("2026-06-25 09:00", closes=[52, 51, 50, 49, 48, 47],
                  highs=[53, 52, 51, 50, 49, 48], lows=[51, 50, 49, 48, 47, 46])
    th = thesis(bias="UP", dol_price=999.0)
    d = decision("2026-06-25 09:00", th, sess_lo=48.0)
    on = run_lifecycle(d, bars, cfg(safety_nets=("ttl", "opposite_extreme")), SESS_END)
    assert on.cause == "safety_net"
    assert on.cause_detail == "opposite_extreme"
    off = run_lifecycle(d, bars, cfg(safety_nets=("ttl",)), SESS_END)
    assert off.cause == "session_end"


def test_churn_cap_aborts_day():
    fals = [{"type": "price_beyond", "price": 45.0, "side": "below"}]
    bars = mkbars("2026-06-25 09:00", closes=[40] * 400, highs=[40.5] * 400, lows=[39] * 400)

    def always_falsify(trigger):
        return decision(trigger, thesis(bias="UP", dol_price=70.0, falsified_if=fals),
                        decision_id=f"d{trigger.value}")

    res = run_day(bars, always_falsify, cfg(churn_cap=5),
                  pd.Timestamp("2026-06-25 09:00", tz=TZ),
                  pd.Timestamp("2026-06-25 16:59", tz=TZ), date="2026-06-25")
    assert res.day_outcome == "churn_cap"
    assert res.n_calls == 5


def test_session_end_closes_open_lifecycle():
    bars = mkbars("2026-06-25 16:50", closes=[50] * 10, highs=[50.5] * 10, lows=[49.5] * 10)
    d = decision("2026-06-25 16:50", thesis(bias="UP", dol_price=999.0), gate="HIGH")
    lc = run_lifecycle(d, bars, cfg(), SESS_END)
    assert lc.cause == "session_end"
    assert lc.died_ts == SESS_END


def test_failsafe_decision_recorded_as_failsafe():
    # Failsafe = NEUTRAL/LOW, treated as low-conf (waiting). Whether it dies by max_age
    # or spans to session end, the lifecycle is labelled `failsafe` (quality dominates).
    bars = mkbars("2026-06-25 09:00", closes=[50] * 40)
    recall = {"events": [], "max_age_min": 30}
    d = decision("2026-06-25 09:00",
                 thesis(bias="NEUTRAL", dol_price=None, recall=recall, confidence="LOW"),
                 gate="LOW", failsafe=True)
    lc = run_lifecycle(d, bars, cfg(), SESS_END)
    assert lc.stood is False
    assert lc.cause == "failsafe"
    assert lc.died_ts == pd.Timestamp("2026-06-25 09:30", tz=TZ)   # max_age 30m
    # And a failsafe that never reaches max_age still records as failsafe at session end.
    short = mkbars("2026-06-25 16:50", closes=[50] * 10)
    d2 = decision("2026-06-25 16:50",
                  thesis(bias="NEUTRAL", dol_price=None,
                         recall={"events": [], "max_age_min": 0}, confidence="LOW"),
                  gate="LOW", failsafe=True)
    lc2 = run_lifecycle(d2, short, cfg(), SESS_END)
    assert lc2.cause == "failsafe"


def test_failsafe_recall_max_age_re_calls(monkeypatch=None):
    """Plan 11 Phase 4: a failsafed day RE-CALLS at +max_age (failsafe_thesis carries
    recall.max_age_min=60), instead of one call spanning the session. run_day over a
    3-hour span at the 60-min failsafe recall must produce >=2 L1 calls."""
    from schemas import failsafe_thesis
    assert failsafe_thesis()["recall"]["max_age_min"] == 60
    bars = mkbars("2026-06-25 09:00", closes=[50] * 200)     # 200 min > 3× 60
    open_ts = pd.Timestamp("2026-06-25 09:00", tz=TZ)
    end_ts = pd.Timestamp("2026-06-25 12:20", tz=TZ)

    def provider(trigger_ts):
        th = failsafe_thesis()
        return decision(trigger_ts, th, gate="LOW", failsafe=True,
                        decision_id=f"fs_{trigger_ts.strftime('%H%M')}")

    # latency 0 so the recall math is clean; each failsafe dies at born+60 → re-call.
    res = run_day(bars, provider, cfg(latency_sec=0), open_ts, end_ts, "2026-06-25")
    assert res.n_calls >= 2
    assert all(lc.cause == "failsafe" for lc in res.lifecycles)


def test_enforce_default_recall_clamps():
    """Plan 12 Fix 1: the provider-side clamp bakes the effective max_age into the logged
    thesis (0/absent → default 60; smaller respected; larger clamped down)."""
    from run_bench import _enforce_default_recall
    t0 = {"recall": {"events": [], "max_age_min": 0}}
    _enforce_default_recall(t0)
    assert t0["recall"]["max_age_min"] == 60
    t30 = {"recall": {"events": [], "max_age_min": 30}}
    _enforce_default_recall(t30)
    assert t30["recall"]["max_age_min"] == 30
    t600 = {"recall": {"events": [], "max_age_min": 600}}
    _enforce_default_recall(t600)
    assert t600["recall"]["max_age_min"] == 60
    tmiss = {}                                    # no recall block at all → synthesized
    _enforce_default_recall(tmiss)
    assert tmiss["recall"] == {"events": [], "max_age_min": 60}


def test_default_lowconf_recall_re_calls_at_default():
    """Plan 12 Fix 1: a VALID (non-failsafe) low-conf thesis whose model-authored recall
    never fires (max_age 0, no events) is re-called at the code-enforced default (60m) once
    the provider bakes the clamp — not left waiting to session end (07-02 th_04 waited 20.5h).
    """
    from run_bench import _enforce_default_recall
    bars = mkbars("2026-06-25 09:00", closes=[50] * 200)    # 200 min > 3× 60
    open_ts = pd.Timestamp("2026-06-25 09:00", tz=TZ)
    end_ts = pd.Timestamp("2026-06-25 12:20", tz=TZ)

    def provider(trigger_ts):
        th = thesis(bias="NEUTRAL", dol_price=None, confidence="LOW",
                    recall={"events": [], "max_age_min": 0})
        _enforce_default_recall(th)               # provider-side clamp (Design B)
        assert th["recall"]["max_age_min"] == 60
        return decision(trigger_ts, th, gate="LOW",
                        decision_id=f"lc_{trigger_ts.strftime('%H%M')}")

    res = run_day(bars, provider, cfg(latency_sec=0), open_ts, end_ts, "2026-06-25")
    assert res.n_calls >= 2                        # re-called, not one span
    assert res.lifecycles[0].cause == "recall"     # non-failsafe waiting death
    assert res.lifecycles[0].stood is False
    assert res.lifecycles[0].died_ts == open_ts + pd.Timedelta(minutes=60)
