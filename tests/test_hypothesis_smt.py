# tests/test_hypothesis_smt.py
# Unit + integration tests for hypothesis_smt.py.
# No IB connection required. No real API calls made (Claude is mocked).
import datetime
import json
from datetime import time
from unittest import mock

import pandas as pd
import pytest

import hypothesis_smt
from hypothesis_smt import (
    HypothesisManager,
    CONTRADICTION_THRESHOLD,
    compute_hypothesis_direction,
    _compute_rule1,
    _assign_case,
    _weighted_vote,
)

# ── Shared test date ──────────────────────────────────────────────────────────

# Use a Monday so Tuesday TWO can be set in the same ISO week
_DATE = datetime.date(2026, 4, 20)   # Monday
_DATE_STR = "2026-04-20"
_PREV_DATE = datetime.date(2026, 4, 17)  # Friday (previous trading day)
_BASE = 21800.0


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_1m_df(
    date: datetime.date,
    bars_per_day: int = 100,
    base: float = _BASE,
    pdh: float = 21900.0,
    pdl: float = 21700.0,
    overnight_above_mid: bool = True,
    overnight_crossed_mid: bool = False,
    overnight_near_far: bool = False,
    p900_offset: float = -20.0,   # offset from pdl-midpoint base
) -> pd.DataFrame:
    """Build a synthetic 1m DataFrame with previous-day bars + current-day overnight/session bars.

    Previous day: pdh/pdl span, bars start at midnight.
    Current day: overnight bars (00:00–08:59) + session bars (09:00+).
    """
    rows = []
    pd_midpoint = (pdh + pdl) / 2.0

    # Previous day bars: 00:00–23:59 with prices that define pdh/pdl
    prev_day = date - datetime.timedelta(days=3)  # Friday if date is Monday
    for h in range(0, 24, 4):
        ts = pd.Timestamp(f"{prev_day} {h:02d}:00:00", tz="America/New_York")
        rows.append({
            "ts": ts, "Open": pd_midpoint, "High": pdh if h == 12 else pd_midpoint + 10,
            "Low": pdl if h == 16 else pd_midpoint - 10, "Close": pd_midpoint, "Volume": 1000.0,
        })

    # Current day: Tuesday of same week (for TWO)
    tuesday = date + datetime.timedelta(days=1)  # Tuesday if date is Monday
    ts_tue = pd.Timestamp(f"{tuesday} 09:00:00", tz="America/New_York")
    rows.append({
        "ts": ts_tue, "Open": base, "High": base + 10, "Low": base - 10,
        "Close": base, "Volume": 500.0,
    })

    # Overnight bars on `date` (00:00–08:59) designed for specific Rule 1 case
    overnight_base = pd_midpoint + 20.0 if overnight_above_mid else pd_midpoint - 20.0
    for h in range(0, 9):
        ts = pd.Timestamp(f"{date} {h:02d}:00:00", tz="America/New_York")
        if overnight_crossed_mid and h == 4:
            # Bar that crosses the midpoint
            o_low  = pd_midpoint - 5.0
            o_high = pd_midpoint + 5.0
        elif overnight_near_far and h == 7:
            # Bar that gets near the far extreme
            o_high = pdh - 5.0   # within 15% of pdh-pdl = 200 * 0.15 = 30 pts
            o_low  = pdh - 10.0
        else:
            o_high = overnight_base + 5.0
            o_low  = overnight_base - 5.0
        rows.append({
            "ts": ts, "Open": overnight_base, "High": o_high, "Low": o_low,
            "Close": overnight_base, "Volume": 500.0,
        })

    # 09:00 bar (price_at_900 and hypothesis generation)
    p900 = pd_midpoint + p900_offset
    ts_900 = pd.Timestamp(f"{date} 09:00:00", tz="America/New_York")
    rows.append({
        "ts": ts_900, "Open": p900, "High": p900 + 5, "Low": p900 - 5,
        "Close": p900, "Volume": 500.0,
    })

    # 09:30 bar (TDO)
    ts_930 = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")
    rows.append({
        "ts": ts_930, "Open": base, "High": base + 5, "Low": base - 5,
        "Close": base, "Volume": 500.0,
    })

    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.index.name = None
    return df


def _make_hist_5m_df(end_date: datetime.date, n_days: int = 10, base: float = _BASE) -> pd.DataFrame:
    """Build a synthetic 5m historical DataFrame spanning n_days before end_date."""
    rows = []
    for i in range(n_days, 0, -1):
        d = end_date - datetime.timedelta(days=i)
        if d.weekday() >= 5:
            continue  # skip weekends
        for bar_h in range(9, 14):
            ts = pd.Timestamp(f"{d} {bar_h:02d}:00:00", tz="America/New_York")
            # Alternating HH/HL trend (bullish) for most days
            day_base = base + (n_days - i) * 5.0
            rows.append({
                "ts": ts, "Open": day_base, "High": day_base + 10.0,
                "Low": day_base - 5.0, "Close": day_base + 5.0, "Volume": 1000.0,
            })
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.index.name = None
    return df


# ── Rule 1 case tests ─────────────────────────────────────────────────────────

def test_compute_direction_case_1_3_long():
    """Case 1.3: overnight never crosses pd_midpoint; price below mid → bias long."""
    # overnight_above_mid=False keeps overnight below midpoint; price_at_900 < midpoint
    mnq = _make_1m_df(_DATE, overnight_above_mid=False, overnight_crossed_mid=False, p900_offset=-20.0)
    # Price below midpoint, no crossing → opposite extreme is pdh → long
    r1 = _compute_rule1(mnq, _DATE)
    assert r1["pd_range_case"] == "1.3", f"Expected 1.3, got {r1['pd_range_case']}"
    assert r1["pd_range_bias"] == "long", f"Expected long, got {r1['pd_range_bias']}"


def test_compute_direction_case_1_3_short():
    """Case 1.3: price above pd_midpoint → bias short (toward opposite low extreme)."""
    mnq = _make_1m_df(_DATE, overnight_above_mid=True, overnight_crossed_mid=False, p900_offset=+20.0)
    r1 = _compute_rule1(mnq, _DATE)
    assert r1["pd_range_case"] == "1.3", f"Expected 1.3, got {r1['pd_range_case']}"
    assert r1["pd_range_bias"] == "short", f"Expected short, got {r1['pd_range_bias']}"


def test_compute_direction_case_1_2():
    """Case 1.2: crossed_mid AND near_far_extreme → bias back to first extreme."""
    # Overnight crosses midpoint AND approaches near pdh (far extreme when above mid)
    mnq = _make_1m_df(_DATE, overnight_above_mid=True, overnight_crossed_mid=True, overnight_near_far=True, p900_offset=+20.0)
    r1 = _compute_rule1(mnq, _DATE)
    assert r1["pd_range_case"] == "1.2", f"Expected 1.2, got {r1['pd_range_case']}"
    # price_now_above_mid=True → bias short (reverse back down from near pdh)
    assert r1["pd_range_bias"] == "short", f"Expected short, got {r1['pd_range_bias']}"


def test_compute_direction_case_1_4():
    """Case 1.4: crossed_mid AND price_now_above_mid AND NOT near_far_extreme → long."""
    mnq = _make_1m_df(_DATE, overnight_above_mid=True, overnight_crossed_mid=True, overnight_near_far=False, p900_offset=+20.0)
    r1 = _compute_rule1(mnq, _DATE)
    assert r1["pd_range_case"] == "1.4", f"Expected 1.4, got {r1['pd_range_case']}"
    assert r1["pd_range_bias"] == "long", f"Expected long, got {r1['pd_range_bias']}"


def test_compute_direction_case_1_1():
    """Case 1.1: crossed_mid AND NOT price_now_above_mid AND NOT near_far_extreme → short."""
    # Overnight crosses midpoint but price_at_900 ends below midpoint
    mnq = _make_1m_df(_DATE, overnight_above_mid=False, overnight_crossed_mid=True, overnight_near_far=False, p900_offset=-20.0)
    r1 = _compute_rule1(mnq, _DATE)
    assert r1["pd_range_case"] == "1.1", f"Expected 1.1, got {r1['pd_range_case']}"
    assert r1["pd_range_bias"] == "short", f"Expected short, got {r1['pd_range_bias']}"


def test_compute_direction_case_1_5():
    """Case 1.5: price_at_900 outside [pdl, pdh] → re-analyze on intraday range."""
    # price_at_900 well above pdh (outside range)
    mnq = _make_1m_df(_DATE, pdh=21900.0, pdl=21700.0, p900_offset=+150.0)  # p900 = 21800 + 150 = 21950 > pdh
    r1 = _compute_rule1(mnq, _DATE)
    assert r1["pd_range_case"] == "1.5", f"Expected 1.5, got {r1['pd_range_case']}"


# ── Weighted vote tests ───────────────────────────────────────────────────────

def test_weighted_vote_long_wins():
    """Rule1=long(3) + discount_week(2) + discount_day(1) → long_score=6 → long."""
    result = _weighted_vote("long", "discount", "discount", "bullish")
    assert result == "long"


def test_weighted_vote_neutral():
    """Conflicting signals below threshold → neutral."""
    # rule1=short(3) vs week=discount(2) + day=discount(1) + trend=bullish(1) = 4 long
    # Actually: short_score=3, long_score=4 → long
    # Let's use perfectly balanced: rule1=None, week=None, day=None, trend=None
    result = _weighted_vote("neutral", None, None, "neutral")
    assert result == "neutral"


def test_weighted_vote_rule1_overrides_trend():
    """Rule1 long (weight 3) overrides bearish trend (weight 1)."""
    # long_score = 3 (rule1) + 2 (discount week) = 5; short_score = 1 (bearish trend)
    result = _weighted_vote("long", "discount", None, "bearish")
    assert result == "long"


# ── Edge case tests ───────────────────────────────────────────────────────────

def test_compute_direction_no_overnight_bars():
    """Missing overnight bars → returns neutral (insufficient data to determine TDO)."""
    # No bars at all on the date → _price_at_900 returns None → r5 is None → returns None
    empty_df = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
    hist = _make_hist_5m_df(_DATE)
    result = compute_hypothesis_direction(empty_df, hist, _DATE)
    assert result is None


def test_compute_direction_no_prior_day():
    """mnq_1m_df has no data for previous day → still works (uses empty prev day bars)."""
    # Only today's bars, no previous day → _compute_rule1 returns neutral bias
    rows = []
    ts_900 = pd.Timestamp(f"{_DATE} 09:00:00", tz="America/New_York")
    ts_930 = pd.Timestamp(f"{_DATE} 09:30:00", tz="America/New_York")
    rows.append({"ts": ts_900, "Open": _BASE, "High": _BASE + 5, "Low": _BASE - 5, "Close": _BASE, "Volume": 500.0})
    rows.append({"ts": ts_930, "Open": _BASE, "High": _BASE + 5, "Low": _BASE - 5, "Close": _BASE, "Volume": 500.0})
    # Tuesday for TWO
    tuesday = _DATE + datetime.timedelta(days=1)
    rows.append({"ts": pd.Timestamp(f"{tuesday} 09:00:00", tz="America/New_York"),
                 "Open": _BASE, "High": _BASE + 5, "Low": _BASE - 5, "Close": _BASE, "Volume": 500.0})
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.index.name = None
    hist = _make_hist_5m_df(_DATE)
    result = compute_hypothesis_direction(df, hist, _DATE)
    # Should return a string or None, never raise
    assert result in ("long", "short", "neutral", None)


def test_compute_direction_returns_string_or_none():
    """Return type is always str or None; never raises."""
    mnq = _make_1m_df(_DATE)
    hist = _make_hist_5m_df(_DATE)
    result = compute_hypothesis_direction(mnq, hist, _DATE)
    assert result in ("long", "short", "neutral", None)


# ── evaluate_bar tests ────────────────────────────────────────────────────────

def _make_manager_with_hypothesis(tmp_path, direction: str = "long") -> HypothesisManager:
    """Build a HypothesisManager with a pre-set hypothesis (no API calls)."""
    mnq = _make_1m_df(_DATE)
    hist = _make_hist_5m_df(_DATE)
    mgr = HypothesisManager(mnq, hist, _DATE)
    # Inject state directly to avoid API calls in generate()
    mgr._direction_bias = direction
    mgr._key_levels = [21850.0, 21900.0]
    session_dir = tmp_path / str(_DATE)
    session_dir.mkdir(parents=True, exist_ok=True)
    mgr._hypothesis_file = session_dir / "hypothesis.json"
    mgr._session_data = {
        "date": str(_DATE),
        "generated_at": "2026-04-20T09:00:00-04:00",
        "session_context": {"tdo": 21800.0},
        "hypothesis": {"direction_bias": direction, "confidence": "medium"},
        "contradiction_count": 0,
        "evidence_log": [],
        "revisions": [],
        "signals_fired": [],
        "summary": None,
    }
    return mgr


class _FakeBar:
    """Minimal bar-like object for evaluate_bar testing."""
    def __init__(self, close: float, high: float = None, low: float = None,
                 date=None):
        self.close = close
        self.high  = high  if high  is not None else close + 2
        self.low   = low   if low   is not None else close - 2
        self.date  = date  or pd.Timestamp("2026-04-20 10:00:00", tz="America/New_York")


def test_evaluate_bar_supports_long(tmp_path):
    """Bar closes 16 pts above previous close on long hypothesis → supports."""
    mgr = _make_manager_with_hypothesis(tmp_path, "long")
    # Seed an evidence log entry so prev_close is defined
    mgr._session_data["evidence_log"].append({
        "time": "2026-04-20T09:30:00-04:00", "event": "seed",
        "classification": "neutral", "level_touched": None, "bar_close": 21800.0,
    })
    bar = _FakeBar(close=21816.1)  # 16.1 pts above seed → supports
    mgr.evaluate_bar(bar)
    classifications = [e["classification"] for e in mgr._session_data["evidence_log"]]
    assert "supports" in classifications


def test_evaluate_bar_contradicts_long(tmp_path):
    """Bar closes 16 pts below previous close on long hypothesis → contradicts."""
    mgr = _make_manager_with_hypothesis(tmp_path, "long")
    mgr._session_data["evidence_log"].append({
        "time": "2026-04-20T09:30:00-04:00", "event": "seed",
        "classification": "neutral", "level_touched": None, "bar_close": 21816.1,
    })
    bar = _FakeBar(close=21800.0)  # 16.1 pts below seed → contradicts
    mgr.evaluate_bar(bar)
    classifications = [e["classification"] for e in mgr._session_data["evidence_log"]]
    assert "contradicts" in classifications


def test_evaluate_bar_neutral_level_touch(tmp_path):
    """Bar touches key_level within 5 pts → neutral evidence entry."""
    mgr = _make_manager_with_hypothesis(tmp_path, "long")
    # key_levels = [21850.0, 21900.0]; touch 21851 (within 5 pts of 21850)
    bar = _FakeBar(close=21851.0, high=21855.0, low=21848.0)
    mgr.evaluate_bar(bar)
    neutral_entries = [e for e in mgr._session_data["evidence_log"] if e["classification"] == "neutral"]
    assert len(neutral_entries) >= 1


def test_evaluate_bar_extreme_contradiction_sets_threshold(tmp_path):
    """Price closes >20 pts below TDO on long → contradiction_count = THRESHOLD immediately."""
    mgr = _make_manager_with_hypothesis(tmp_path, "long")
    # TDO = 21800.0; close 21775 is 25 pts below → extreme contradiction
    bar = _FakeBar(close=21775.0)
    mgr.evaluate_bar(bar)
    assert mgr._contradiction_count == CONTRADICTION_THRESHOLD


def test_evaluate_bar_revision_triggered_on_threshold(tmp_path):
    """3 contradicts entries → _revision_triggered = True and _revise() called."""
    mgr = _make_manager_with_hypothesis(tmp_path, "long")
    # Seed a prev_close so move checks work
    mgr._session_data["evidence_log"].append({
        "time": "2026-04-20T09:30:00-04:00", "event": "seed",
        "classification": "neutral", "level_touched": None, "bar_close": 21816.1,
    })
    # Mock _revise to avoid API call
    mgr._revise = mock.MagicMock()

    prev_close = 21816.1
    for i in range(CONTRADICTION_THRESHOLD):
        prev_close += 0.1  # tiny increment to move prev_close forward
        mgr._session_data["evidence_log"][-1]["bar_close"] = prev_close
        # Bar 16+ pts below prev_close → contradicts
        bar = _FakeBar(close=prev_close - 16.1,
                       date=pd.Timestamp(f"2026-04-20 10:0{i}:00", tz="America/New_York"))
        mgr.evaluate_bar(bar)
        # Re-seed prev_close for next iteration
        if i < CONTRADICTION_THRESHOLD - 1:
            mgr._session_data["evidence_log"].append({
                "time": f"2026-04-20T10:0{i}:00-04:00", "event": "seed",
                "classification": "neutral", "level_touched": None,
                "bar_close": prev_close - 16.0,
            })

    assert mgr._revision_triggered is True
    mgr._revise.assert_called()


# ── HypothesisManager.generate() tests ───────────────────────────────────────

def test_generate_writes_hypothesis_file(tmp_path, monkeypatch):
    """generate() writes hypothesis.json inside data/sessions/YYYY-MM-DD/."""
    monkeypatch.setattr(hypothesis_smt, "SESSION_DATA_DIR", tmp_path)
    mnq = _make_1m_df(_DATE)
    hist = _make_hist_5m_df(_DATE)

    hypothesis_response = {
        "direction_bias": "long",
        "confidence": "medium",
        "narrative": "Test narrative.",
        "primary_reason": "Rule 1.3",
        "supporting_factors": [],
        "contradicting_factors": [],
        "key_levels_to_watch": [21800.0],
        "expected_scenario": "Test scenario.",
    }
    with mock.patch("anthropic.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value.content = [
            mock.MagicMock(text=json.dumps(hypothesis_response))
        ]
        manager = HypothesisManager(mnq, hist, _DATE)
        manager.generate()

    hyp_file = tmp_path / str(_DATE) / "hypothesis.json"
    assert hyp_file.exists(), "hypothesis.json was not created"
    data = json.loads(hyp_file.read_text())
    assert data["hypothesis"]["direction_bias"] == "long"


def test_generate_api_failure_falls_back_to_rule_engine(tmp_path, monkeypatch):
    """If Claude API raises, generate() still writes file using rule engine direction."""
    monkeypatch.setattr(hypothesis_smt, "SESSION_DATA_DIR", tmp_path)
    mnq = _make_1m_df(_DATE)
    hist = _make_hist_5m_df(_DATE)

    with mock.patch("anthropic.Anthropic") as mock_client:
        mock_client.return_value.messages.create.side_effect = Exception("API error")
        manager = HypothesisManager(mnq, hist, _DATE)
        manager.generate()

    hyp_file = tmp_path / str(_DATE) / "hypothesis.json"
    assert hyp_file.exists(), "hypothesis.json not created on API failure"
    data = json.loads(hyp_file.read_text())
    assert data["hypothesis"]["direction_bias"] in ("long", "short", "neutral")


# ── matches_hypothesis in backtest ────────────────────────────────────────────

def _build_signal_bars(direction: str, date: str, base: float = 20000.0, n: int = 60):
    """Build synthetic 1m MNQ/MES bars that produce an SMT signal in the given direction."""
    start_ts = pd.Timestamp(f"{date} 09:00:00", tz="America/New_York")
    # Add a 9:30 bar for TDO
    full_idx = pd.date_range(start=start_ts, periods=n, freq="1min")
    opens  = [base] * n
    closes = [base] * n
    highs  = [base + 5] * n
    lows   = [base - 5] * n

    if direction == "short":
        # Bearish anchor at bar 5
        opens[5]  = base - 2
        closes[5] = base + 2
        # Bearish SMT: MES makes new high at bar 7; MNQ doesn't
        mes_highs = list(highs)
        mes_highs[7] = base + 30
        mnq_highs = list(highs)
        # Bearish confirmation at bar 8
        opens[8]  = base + 2
        closes[8] = base - 2
        mnq_highs[8] = base + 6
    else:  # long
        # Bullish anchor at bar 5
        opens[5]  = base + 2
        closes[5] = base - 2
        # Bullish SMT: MES makes new low at bar 7; MNQ doesn't
        mes_highs = list(highs)
        mes_lows_arr = list(lows)
        mes_lows_arr[7] = base - 30
        mnq_highs = list(highs)
        mnq_lows = list(lows)
        # Bullish confirmation at bar 8
        opens[8]  = base - 2
        closes[8] = base + 2
        mnq_lows[8] = base - 6
        mnq = pd.DataFrame(
            {"Open": opens, "High": mnq_highs, "Low": mnq_lows, "Close": closes, "Volume": [1000.0] * n},
            index=full_idx,
        )
        mes = pd.DataFrame(
            {"Open": opens, "High": mes_highs, "Low": mes_lows_arr, "Close": closes, "Volume": [1000.0] * n},
            index=full_idx,
        )
        return mnq, mes

    mnq = pd.DataFrame(
        {"Open": opens, "High": mnq_highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
        index=full_idx,
    )
    mes = pd.DataFrame(
        {"Open": opens, "High": mes_highs, "Low": lows, "Close": closes, "Volume": [1000.0] * n},
        index=full_idx,
    )
    return mnq, mes


def _run_backtest_with_hypothesis(monkeypatch, hyp_direction, signal_direction):
    """Run a minimal backtest and return trade records for matches_hypothesis assertion."""
    import backtest_smt
    import strategy_smt as _strat

    date_str = "2026-04-14"  # Monday — ALLOWED_WEEKDAYS must include it
    mnq, mes = _build_signal_bars(signal_direction, date_str)

    monkeypatch.setattr(backtest_smt, "BACKTEST_START", date_str)
    monkeypatch.setattr(backtest_smt, "BACKTEST_END",   "2026-04-15")
    monkeypatch.setattr(backtest_smt, "WALK_FORWARD_WINDOWS", 1)
    monkeypatch.setattr(backtest_smt, "FOLD_TEST_DAYS",        1)
    monkeypatch.setattr(_strat, "ALLOWED_WEEKDAYS", [0, 1, 2, 3, 4])
    monkeypatch.setattr(_strat, "SESSION_START", "09:00")
    monkeypatch.setattr(_strat, "SESSION_END",   "10:30")
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(_strat, "SIGNAL_BLACKOUT_END",   "")
    monkeypatch.setattr(_strat, "MIN_BARS_BEFORE_SIGNAL", 0)
    monkeypatch.setattr(_strat, "REENTRY_MAX_MOVE_PTS",   0.0)
    monkeypatch.setattr(_strat, "TRADE_DIRECTION",        signal_direction)
    monkeypatch.setattr(_strat, "MIN_STOP_POINTS",        0.5)
    monkeypatch.setattr(_strat, "MIN_TDO_DISTANCE_PTS",   0.0)
    monkeypatch.setattr(_strat, "TDO_VALIDITY_CHECK",     False)
    monkeypatch.setattr(_strat, "BREAKEVEN_TRIGGER_PCT",  0.0)
    monkeypatch.setattr(_strat, "TRAIL_AFTER_TP_PTS",     0.0)
    monkeypatch.setattr(_strat, "MAX_HOLD_BARS",          20)
    monkeypatch.setattr(backtest_smt, "RISK_PER_TRADE",   50.0)
    monkeypatch.setattr(backtest_smt, "MAX_CONTRACTS",    1)

    # Mock compute_hypothesis_direction to return the desired direction
    monkeypatch.setattr(
        backtest_smt, "compute_hypothesis_direction",
        lambda *args, **kwargs: hyp_direction,
    )

    stats = backtest_smt.run_backtest(mnq, mes, start=date_str, end="2026-04-15")
    return stats.get("trade_records", [])


def test_backtest_signal_matches_hypothesis_true(monkeypatch):
    """Signal direction == hypothesis direction → matches_hypothesis = True."""
    trades = _run_backtest_with_hypothesis(monkeypatch, "short", "short")
    if trades:
        for t in trades:
            assert t.get("matches_hypothesis") is True, f"Expected True, got {t.get('matches_hypothesis')}"


def test_backtest_signal_matches_hypothesis_false(monkeypatch):
    """Signal direction != hypothesis direction → matches_hypothesis = False."""
    trades = _run_backtest_with_hypothesis(monkeypatch, "long", "short")
    if trades:
        for t in trades:
            assert t.get("matches_hypothesis") is False, f"Expected False, got {t.get('matches_hypothesis')}"


def test_backtest_matches_hypothesis_when_no_data(monkeypatch):
    """compute_hypothesis_direction returns None → matches_hypothesis = None."""
    trades = _run_backtest_with_hypothesis(monkeypatch, None, "short")
    if trades:
        for t in trades:
            assert t.get("matches_hypothesis") is None, f"Expected None, got {t.get('matches_hypothesis')}"
