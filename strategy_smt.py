"""strategy_smt.py — SMT Divergence strategy constants and functions. Fully mutable — owned by the optimizing agent."""
import datetime
import json
import os
import sys
from pathlib import Path

import pandas as pd

# Cache directory for futures parquet files.
FUTURES_CACHE_DIR = os.environ.get(
    "FUTURES_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "futures_data"),
)

# ══ STRATEGY TUNING ══════════════════════════════════════════════════════════
# Kill zone: NY open session window (America/New_York).
SESSION_START = "09:00"
SESSION_END   = "13:30"

# Minimum wall-clock minutes before a divergence signal can fire after session open.
# Used as a timedelta in screen_session, so it is interval-agnostic.
# Set 0 to disable (bar 0 is naturally suppressed by the empty prior-session slice
# in detect_smt_divergence, so the first real signal opportunity is bar 1 regardless).
MIN_BARS_BEFORE_SIGNAL = 0

# Direction filter: "both" = trade longs and shorts | "long" = longs only | "short" = shorts only
# Re-testing "both" with quality filters active (MAX_TDO=15, STOP_RATIO=0.35).
# Previous short-only verdict was pre-filter; longs+Thursdays now evaluated on equal footing.
TRADE_DIRECTION = "both"

# TDO validity gate: skip signals where the take-profit target is geometrically inverted.
# For LONG: TDO must be above entry (price bounces up to the open).
# For SHORT: TDO must be below entry (price fades down to the open).
# Set False to disable and restore legacy behavior.
TDO_VALIDITY_CHECK = True

# Minimum stop distance in MNQ points. Signals with |entry - stop| < this value are skipped.
# Prevents degenerate sizing when TDO is very close to entry.
# Set 0.0 to disable.
MIN_STOP_POINTS = 2.5

# Per-direction stop placement ratios (fraction of |entry - TDO| distance).
#
# SHORT_STOP_RATIO: optimizer search space [0.25, 0.30, 0.35, 0.40, 0.45] (step 0.05).
# 0.05 was noise-level tight (~3 pts on a 20–40 pt wick instrument → 95% stop-outs).
# Widening to 0.25+ reduces contracts (via position sizer) but raises win rate enough
# to turn expected value positive.
#
# LONG_STOP_RATIO: frozen at 0.05 — longs disabled (TRADE_DIRECTION = "short"),
# value is irrelevant but kept valid to avoid breaking the position-sizing path.
LONG_STOP_RATIO  = 0.35
SHORT_STOP_RATIO = 0.35

# MNQ futures P&L per point per contract.
MNQ_PNL_PER_POINT = 2.0

# Re-entry after mid-session stop-out: allow a second entry on the same divergence.
# Measures how far price moved in the target direction from entry before the stop hit.
# For shorts: move = entry_price − exit_close. If move < threshold, the setup is still
# "loaded" and a new confirmation bar qualifies for re-entry.
# Set 0.0 to disable re-entry entirely.
# Optimizer search space: [0.0, 5.0, 10.0, 20.0, 30.0].
REENTRY_MAX_MOVE_PTS = 999.0

# Ratio-based reentry invalidation: replaces the hard REENTRY_MAX_MOVE_PTS sentinel.
# move_threshold = REENTRY_MAX_MOVE_RATIO * |entry - TDO|. Scale-invariant alternative.
# 0.5 = "price moved more than 50% toward TP before stopping out" → disqualify re-entry.
# Set >= 99 to effectively disable (9999-pt threshold).
# Optimizer search space: [0.25, 0.5, 0.75, 99].
REENTRY_MAX_MOVE_RATIO: float = 0.5

# Pre-TDO progress-based stop lock-in (replaces BREAKEVEN_TRIGGER_PTS).
# Fraction of |entry − TDO| price must travel before stop is moved to entry (breakeven).
# Scale-invariant: 0.65 means "65% of the way to TDO regardless of trade size."
# 0.0 = disable (stop frozen pre-TDO, matching current behaviour).
# Optimizer search space: [0.0, 0.50, 0.60, 0.65, 0.70, 0.75].
BREAKEVEN_TRIGGER_PCT = 0.0

# Maximum bars a trade may remain open after entry (0 = disabled).
# Applies per trade, including re-entries. Exits as "exit_time" at bar N+MAX_HOLD_BARS.
MAX_HOLD_BARS = 120

# Minimum TDO distance filter: skip signals where |entry - TDO| < this value in MNQ pts.
# Filters out setups where TDO is very close to entry.
# Walk-forward evidence: close-TDO setups are net profitable; 15 is the empirically best floor.
# Set 0.0 to disable.
# Optimizer search space: [0.0, 10.0, 15.0, 20.0, 25.0].
MIN_TDO_DISTANCE_PTS = 0.0

# Allowed weekdays for trading (Python weekday: Mon=0 … Sun=6).
# Thursday (3) excluded: 25% win rate vs 40.8% for all other days (Finding 2).
# Set frozenset({0,1,2,3,4}) to re-enable all weekdays.
ALLOWED_WEEKDAYS = frozenset({0, 1, 2, 3, 4})

# Signal blackout window: skip divergence signals whose entry bar falls in this time range.
# Both values are "HH:MM" strings in the session's local timezone; "" disables the filter.
# Blocks 11:00–13:30: 11:xx dead zone + 13:xx drag (only negative-PnL slot, Finding 3).
# Optimizer search space: ["", "11:00"] for START; ["", "13:00", "13:30"] for END.
SIGNAL_BLACKOUT_START = "11:00"
SIGNAL_BLACKOUT_END   = "13:00"

# Trail-after-TP: instead of exiting at TDO, convert TP into a trailing stop.
# When price first crosses TDO the position stays open; the stop is then trailed
# this many points behind the best post-TDO price. Set 0.0 to disable (exit at TDO).
# Optimizer search space: [0.0, 5.0, 10.0, 20.0].
TRAIL_AFTER_TP_PTS = 1.0

# Maximum TDO distance filter: skip signals where |entry - TDO| > this value in MNQ pts.
# Cross-tab finding: TDO<20 has WR=37-43% and EP=$32-$59 across ALL re-entry sequences,
# including 5th+. TDO>100 trades are structurally losing (EP=−$2.04). TDO>50 barely break
# even. The quality degradation at high re-entry counts is driven by TDO distance, not depth.
# Optimizer search space: [15, 20, 25, 30, 40, 999].
# Set 999.0 to disable (pass-through for all distances).
MAX_TDO_DISTANCE_PTS = 15.0

# Maximum re-entries per session day.
# At TDO<20 (with MAX_TDO_DISTANCE_PTS applied), even Seq#5+ has EP=$32, so this filter
# is less important than expected. Most useful at TDO 20-50 where Seq#5+ declines to EP=$6.
# Optimizer search space: [1, 2, 3, 4, 999]. Default 999 = disabled.
# Semantic note: in signal_smt this counts repeated divergence SIGNALS on the same level;
# in backtest_smt it counts ENTRY attempts. The two counts can differ when a signal fires
# but fails a filter (TDO_VALIDITY_CHECK, MIN_STOP_POINTS, etc.) before entry.
MAX_REENTRY_COUNT = 1

# Minimum bars the prior trade must have survived before re-entry is allowed.
# DIAGNOSTIC ONLY — do not include in optimization runs. Extended diagnostics showed:
# prior_bars<3 (n=1036, 42% of re-entries) has EP=$16.39 — removing these hurts volume
# without improving EP. WR bumps at 10+ bars but EP stays flat. At TDO<20, prior duration
# is irrelevant. Set 0 to disable (always allow re-entry).
MIN_PRIOR_TRADE_BARS_HELD = 0

# Minimum MES sweep magnitude for SMT divergence: how far MES must exceed the prior
# session extreme to qualify. Marginal sweeps (< 1 pt) are noise.
# Optimizer search space: [0, 1, 2, 5].
# Set 0.0 to disable.
MIN_SMT_SWEEP_PTS = 0.0

# Minimum MNQ miss magnitude for SMT divergence: how far MNQ must fail to match MES.
# A strong divergence (MNQ missed by 3 pts) is more reliable than a marginal one (0.5 pt).
# Optimizer search space: [0, 1, 2, 5].
# Set 0.0 to disable.
MIN_SMT_MISS_PTS = 0.0

# Midnight open as TP target (replaces 9:30 RTH open / TDO).
# ICT canonical intraday reversion target = first 1m bar at/after 00:00 ET.
# Default True: pre-9:30 signals no longer reference a future (post-signal) price.
# Optimizer search space: [True, False]
MIDNIGHT_OPEN_AS_TP: bool = True

# Pessimistic fill simulation: use bar extreme as exit price instead of exact level.
# Stop-outs fill at bar Low (long) / bar High (short); TPs fill at bar High (long) / bar Low (short).
# Note: TP fill at bar High (long) / bar Low (short) is technically optimistic — it assumes
# price reaches the bar extreme in our favor. Kept for consistency with stop fill behavior.
# Default True so optimizer always sees consistent, realistic fills.
# Optimizer search space: [True, False]
PESSIMISTIC_FILLS: bool = True

# Structural stop placement: stop beyond the divergence bar's wick extreme.
# When False: ratio × |entry - TP| (current behavior).
# STRUCTURAL_STOP_BUFFER_PTS: points beyond the wick to place the stop.
# Optimizer search space: STRUCTURAL_STOP_MODE [True, False];
#   STRUCTURAL_STOP_BUFFER_PTS [1.0, 2.0, 3.0, 5.0]
STRUCTURAL_STOP_MODE: bool = False
STRUCTURAL_STOP_BUFFER_PTS: float = 2.0

# Thesis-invalidation exits (close-based; fires before stop check).
# MSS: close beyond the divergence bar's wick extreme on the entry instrument.
# CISD: close beyond the midnight open (requires MIDNIGHT_OPEN_AS_TP = True).
# SMT: close beyond the MNQ level that defined the divergence (defended level).
# All optimizer search space: [True, False]
INVALIDATION_MSS_EXIT: bool = False
INVALIDATION_CISD_EXIT: bool = False
INVALIDATION_SMT_EXIT: bool = False

# Overnight sweep gate: require overnight H (for shorts) or L (for longs)
# to have been swept before the signal bar fires.
# OVERNIGHT_RANGE_AS_TP: use opposite overnight extreme as TP instead of TDO/midnight.
# Optimizer search space: [True, False]
OVERNIGHT_SWEEP_REQUIRED: bool = False
OVERNIGHT_RANGE_AS_TP: bool = False

# Silver Bullet window: restrict new divergence detection to 09:50–10:10 ET.
# Re-entries allowed outside window if original divergence was inside it.
# Optimizer search space: [True, False]
SILVER_BULLET_WINDOW_ONLY: bool = False
SILVER_BULLET_START = "09:50"
SILVER_BULLET_END   = "10:10"

# Hidden SMT: body/close-based divergence (MES close new session extreme,
# MNQ close does not). Only fires if wick SMT did not fire on the same bar.
# Optimizer search space: [True, False]
# Approved in Round 1 experiments: +30.6% PnL, lower drawdown, same signal quality.
HIDDEN_SMT_ENABLED: bool = True

# Two-layer position model: enter Layer A at LAYER_A_FRACTION of max contracts,
# add Layer B when price retraces into the FVG zone.
# LAYER_A_FRACTION = 0.5 means Layer A gets floor(total * 0.5) contracts; Layer B gets the rest.
# Requires FVG_ENABLED and FVG_LAYER_B_TRIGGER to also be True for Layer B to enter.
# Optimizer search space: TWO_LAYER_POSITION [True, False]; LAYER_A_FRACTION [0.33, 0.5, 0.67]
TWO_LAYER_POSITION: bool = False
LAYER_A_FRACTION: float = 0.5

# FVG (Fair Value Gap) detection: 3-bar imbalance where bar3 and bar1 do not overlap.
# FVG_MIN_SIZE_PTS: minimum gap size in MNQ points to qualify as a valid FVG.
# FVG_LAYER_B_TRIGGER: when True + TWO_LAYER_POSITION, Layer B enters on FVG retracement.
# Optimizer search space: FVG_ENABLED [True, False]; FVG_MIN_SIZE_PTS [1.0, 2.0, 3.0, 5.0]
FVG_ENABLED: bool = False
FVG_MIN_SIZE_PTS: float = 2.0
FVG_LAYER_B_TRIGGER: bool = False

# SMT-optional: accept displacement candles (body ≥ MIN_DISPLACEMENT_PTS) as entries
# even when no wick-based SMT exists. Fires only when detect_smt_divergence returns None.
# Optimizer search space: SMT_OPTIONAL [True, False]; MIN_DISPLACEMENT_PTS [8.0, 10.0, 15.0]
SMT_OPTIONAL: bool = True
MIN_DISPLACEMENT_PTS: float = 10.0

# Partial exit at first draw on liquidity.
# PARTIAL_EXIT_FRACTION: fraction of open contracts to close at the partial level.
# Partial level = midpoint between entry and take_profit.
# Optimizer search space: PARTIAL_EXIT_ENABLED [True, False]; PARTIAL_EXIT_FRACTION [0.33, 0.5]
PARTIAL_EXIT_ENABLED: bool = True   # Round 2 approved: -12% drawdown, minimal P&L cost
PARTIAL_EXIT_FRACTION: float = 0.33  # Round 2 approved: 0.33 outperformed 0.5 (B-1 vs B-2)

# SMT fill divergence: MES fills a FVG that MNQ has not (bearish) or vice versa (bullish).
# Fires as an alternative to wick SMT in the IDLE detection loop.
# Optimizer search space: [True, False]
SMT_FILL_ENABLED: bool = False

# ── Displacement entry quality controls (Plan 3) ──────────────────────────────
# Re-enables SMT_OPTIONAL experiments after adding the two fixes that Round 2
# identified as prerequisites: correct stop placement and hypothesis score gate.
#
# DISPLACEMENT_STOP_MODE: when True and smt_type=="displacement", sets initial
#   stop to the displacement bar's extreme (bar Low for long, bar High for short)
#   instead of the SMT structural stop. Mechanistically correct: the displacement
#   thesis fails when price closes back through the impulse bar.
#   Optimizer search space: [True, False]
DISPLACEMENT_STOP_MODE: bool = True

# MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT: minimum count of hypothesis rules that
#   must agree with signal direction for a displacement entry to be accepted.
#   0 = gate disabled (all displacement entries pass). Only effective when
#   SMT_OPTIONAL=True; has no effect on wick/body SMT entries.
#   Score range: 0–4 (pd_range_bias, week_zone, day_zone, trend_direction votes).
#   Optimizer search space: [0, 2, 3]
MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT: int = 0

# ── Partial exit level (Plan 3) ───────────────────────────────────────────────
# PARTIAL_EXIT_LEVEL_RATIO: linear interpolation between entry (0.0) and TP (1.0)
#   for the partial exit target. 0.5 = current hardcoded midpoint (no behaviour
#   change when PARTIAL_EXIT_ENABLED=True). Enables Round 3 experiments at
#   0.33 (earlier lock-in, higher probability) and 0.67 (later, more favorable RR).
#   Only read by manage_position() when PARTIAL_EXIT_ENABLED=True.
#   Optimizer search space: [0.33, 0.5, 0.67]
PARTIAL_EXIT_LEVEL_RATIO: float = 0.33

# ── Layer B hypothesis gate (Plan 3) ─────────────────────────────────────────
# FVG_LAYER_B_REQUIRES_HYPOTHESIS: when True, the FVG retracement add-on (Layer B)
#   is only accepted in sessions where hypothesis_score >= MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT.
#   ICT's two-leg accumulation model is a reversal/pullback structure — gating it to
#   hypothesis-confirmed sessions prevents Layer B entries in pure momentum days.
#   Requires TWO_LAYER_POSITION=True; has no effect when FVG is disabled.
#   Optimizer search space: [True, False]
FVG_LAYER_B_REQUIRES_HYPOTHESIS: bool = False

# ── Structural signal quality (Plan 4) ───────────────────────────────────────

# Symmetric SMT: also detect divergences where MNQ leads and MES fails (not only MES-leads).
# smt_type = "wick_sym" for MNQ-leads signals.
# Optimizer search space: [True, False]
SYMMETRIC_SMT_ENABLED: bool = False

# Displacement body size filter: minimum |Close - Open| of the displacement bar.
# 0.0 = disabled. displacement_body_pts is always recorded as a diagnostic field.
# Optimizer search space: [0.0, 8.0, 12.0, 15.0]
MIN_DISPLACEMENT_BODY_PTS: float = 0.0

# Always-on confirmation candle: require the confirmation bar to break the displacement
# bar's body boundary even for non-delayed entries.
# Optimizer search space: [True, False]
ALWAYS_REQUIRE_CONFIRMATION: bool = False

# Expanded reference levels: check sweeps against quarterly sessions, calendar day,
# and current calendar week H/L in addition to the current session extreme.
# Optimizer search space: [True, False]
EXPANDED_REFERENCE_LEVELS: bool = False

# HTF visibility filter: suppress signals not visible on any of the configured HTF periods.
# HTF_PERIODS_MINUTES: list of timeframe widths (minutes) to check.
# Optimizer search space: HTF_VISIBILITY_REQUIRED [True, False]
HTF_VISIBILITY_REQUIRED: bool = False
HTF_PERIODS_MINUTES: list = [15, 30, 60, 240]

# ── Solution A+B: Divergence quality scoring and hypothesis replacement ────────
MIN_DIV_SCORE:             float = 0.0    # minimum score to accept divergence; 0 = off
REPLACE_THRESHOLD:         float = 1.5    # new_score must be > pending_effective * this to replace

# Score decay — pending hypothesis grows easier to replace the longer it is held
DIV_SCORE_DECAY_FACTOR:    float = 0.90   # per-interval multiplier; 1.0 = disabled
DIV_SCORE_DECAY_INTERVAL:  int   = 10     # bars between each decay step

# Adverse-move decay — additional decay based on how far price moved against hypothesis
ADVERSE_MOVE_FULL_DECAY_PTS:  float = 150.0  # pts of adverse move that drives score to floor; 999 = disabled
ADVERSE_MOVE_MIN_DECAY:       float = 0.10   # floor for the adverse-move decay multiplier

# ── Solution D: Hard invalidation threshold ───────────────────────────────────
HYPOTHESIS_INVALIDATION_PTS:  float = 999.0  # abandon hypothesis after this many pts adverse; 999 = disabled


# ── Module-level bar data ─────────────────────────────────────────────────────
_mnq_bars: "pd.DataFrame | None" = None
_mes_bars: "pd.DataFrame | None" = None


def set_bar_data(mnq_df: pd.DataFrame, mes_df: pd.DataFrame) -> None:
    """Populate module-level bar globals for strategy functions that need lookback.

    Reserved for multi-bar lookback logic (e.g. prior-session anchor, ATR filter).
    Called by run_backtest() and both 1m bar callbacks in signal_smt.
    """
    global _mnq_bars, _mes_bars
    _mnq_bars = mnq_df
    _mes_bars = mes_df


# ══ STRATEGY FUNCTIONS ═══════════════════════════════════════════════════════

def _load_futures_manifest() -> dict:
    """Load futures_manifest.json written by prepare_futures.py."""
    path = Path(FUTURES_CACHE_DIR) / "futures_manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No futures_manifest.json at {path}. Run prepare_futures.py first."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_futures_data() -> dict[str, pd.DataFrame]:
    """Load MNQ and MES futures parquets.

    Checks in priority order:
      1. data/historical/{ticker}_{interval}.parquet  — interval-specific Databento file
      2. data/historical/{ticker}.parquet             — default Databento file
      3. FUTURES_CACHE_DIR/{interval}/{ticker}.parquet — IB ephemeral cache
    Returns {"MNQ": df, "MES": df} with tz-aware ET DatetimeIndex.
    Raises FileNotFoundError if parquets are missing (run prepare_futures.py).
    """
    manifest = _load_futures_manifest()
    interval = manifest.get("fetch_interval", "1m")
    result: dict[str, pd.DataFrame] = {}
    for ticker in ["MNQ", "MES"]:
        interval_path   = Path("data/historical") / f"{ticker}_{interval}.parquet"
        historical_path = Path("data/historical") / f"{ticker}.parquet"
        ib_path         = Path(FUTURES_CACHE_DIR) / interval / f"{ticker}.parquet"
        if interval_path.exists():
            path = interval_path
        elif historical_path.exists():
            path = historical_path
        elif ib_path.exists():
            path = ib_path
        else:
            raise FileNotFoundError(
                f"Missing futures parquet for {ticker}. Run prepare_futures.py."
            )
        result[ticker] = pd.read_parquet(path)
    # Align MNQ and MES to their common timestamps so run_backtest can apply
    # a single session_mask across both DataFrames without a length mismatch.
    # Bars missing from either instrument are silently dropped from both —
    # correct for SMT divergence which requires simultaneous bars.
    if "MNQ" in result and "MES" in result:
        common_idx = result["MNQ"].index.intersection(result["MES"].index)
        result["MNQ"] = result["MNQ"].loc[common_idx]
        result["MES"] = result["MES"].loc[common_idx]
    return result



def divergence_score(
    sweep_pts: float,
    miss_pts: float,
    body_pts: float,
    smt_type: str,
    hypothesis_direction: "str | None",
    div_direction: str,
) -> float:
    """Score a divergence [0, 1]. Higher = stronger, harder to displace."""
    if smt_type == "displacement":
        base = min(body_pts / 20.0, 1.0)
    else:
        base = (
            min(sweep_pts / 5.0,  1.0) * 0.25
            + min(miss_pts  / 25.0, 1.0) * 0.50
            + min(body_pts  / 15.0, 1.0) * 0.25
        )
    if hypothesis_direction not in (None, "neutral"):
        if div_direction == hypothesis_direction:
            base = min(base + 0.20, 1.0)
    return base


def _effective_div_score(
    score: float,
    discovery_bar_idx: int,
    current_bar_idx: int,
    discovery_price: float,
    direction: str,
    current_bar_high: float,
    current_bar_low: float,
) -> float:
    """Apply time and adverse-move decay to a pending divergence score."""
    bars_held  = current_bar_idx - discovery_bar_idx
    time_decay = DIV_SCORE_DECAY_FACTOR ** (bars_held // max(DIV_SCORE_DECAY_INTERVAL, 1))

    if direction == "short":
        adverse = max(0.0, current_bar_high - discovery_price)
    else:
        adverse = max(0.0, discovery_price - current_bar_low)
    move_decay = max(
        ADVERSE_MOVE_MIN_DECAY,
        1.0 - adverse / max(ADVERSE_MOVE_FULL_DECAY_PTS, 1.0),
    )
    return score * time_decay * move_decay


def detect_smt_divergence(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    session_start_idx: int,
    _min_bars: int = 0,
    _cached: "dict | None" = None,
) -> tuple[str, float, float, str, float] | None:
    """Check for SMT divergence at bar_idx.

    Returns tuple (direction, sweep_pts, miss_pts, smt_type, smt_defended_level) or None.
    - direction: "short" if MES makes new session high but MNQ does not;
                 "long"  if MES makes new session low  but MNQ does not.
    - sweep_pts: how far MES exceeded the session extreme (always >= 0)
    - miss_pts:  how far MNQ failed to match MES (always >= 0)
    - smt_type: "wick" for high/low-based divergence; "body" for close-based (hidden SMT)
    - smt_defended_level: MNQ session extreme MNQ failed to match
    Returns None if no divergence, bar-count guard fires, or sweep/miss filters reject.

    Args:
        mes_bars: OHLCV DataFrame for MES, index = ET datetime (any bar interval)
        mnq_bars: OHLCV DataFrame for MNQ, same index alignment
        bar_idx: current bar position in the session slice
        session_start_idx: first bar index of current session
        _min_bars: Skip bars where bar_idx - session_start_idx < _min_bars.
            Default 0 disables the guard — callers should apply their own
            time-based threshold (e.g. screen_session uses MIN_BARS_BEFORE_SIGNAL
            as a wall-clock timedelta, which is interval-agnostic).
        _cached: optional dict with pre-computed session extremes for this bar:
            keys "mes_h", "mes_l", "mnq_h", "mnq_l" (wick-based),
            and "mes_ch", "mes_cl", "mnq_ch", "mnq_cl" (close-based for HIDDEN_SMT).
            When provided, skips the O(n) slice.max/min computation.
            Values should be nan (not -inf/+inf) when the session is empty (bar 0).
    """
    if bar_idx - session_start_idx < _min_bars:
        return None

    # Session extremes: use pre-computed values if available, else compute from slice
    if _cached is not None:
        mes_session_high = _cached["mes_h"]
        mes_session_low  = _cached["mes_l"]
        mnq_session_high = _cached["mnq_h"]
        mnq_session_low  = _cached["mnq_l"]
    else:
        session_slice = slice(session_start_idx, bar_idx)
        mes_session_high = mes_bars["High"].iloc[session_slice].max()
        mes_session_low  = mes_bars["Low"].iloc[session_slice].min()
        mnq_session_high = mnq_bars["High"].iloc[session_slice].max()
        mnq_session_low  = mnq_bars["Low"].iloc[session_slice].min()

    cur_mes = mes_bars.iloc[bar_idx]
    cur_mnq = mnq_bars.iloc[bar_idx]

    # Bearish SMT: MES sweeps session high (liquidity grab) but MNQ fails to confirm
    if cur_mes["High"] > mes_session_high and cur_mnq["High"] <= mnq_session_high:
        smt_sweep = cur_mes["High"] - mes_session_high
        mnq_miss   = mnq_session_high - cur_mnq["High"]
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("short", smt_sweep, mnq_miss, "wick", mnq_session_high)
    # Bullish SMT: MES sweeps session low but MNQ fails to confirm
    if cur_mes["Low"] < mes_session_low and cur_mnq["Low"] >= mnq_session_low:
        smt_sweep = mes_session_low - cur_mes["Low"]
        mnq_miss   = cur_mnq["Low"] - mnq_session_low
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("long", smt_sweep, mnq_miss, "wick", mnq_session_low)

    # Symmetric SMT: MNQ leads, MES fails (mirror of MES-leads logic above).
    if SYMMETRIC_SMT_ENABLED:
        if cur_mnq["High"] > mnq_session_high and cur_mes["High"] <= mes_session_high:
            smt_sweep = cur_mnq["High"] - mnq_session_high
            mnq_miss   = mnq_session_high - cur_mes["High"]
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("short", smt_sweep, mnq_miss, "wick_sym", mes_session_high)
        if cur_mnq["Low"] < mnq_session_low and cur_mes["Low"] >= mes_session_low:
            smt_sweep = mnq_session_low - cur_mnq["Low"]
            mnq_miss   = cur_mes["Low"] - mes_session_low
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("long", smt_sweep, mnq_miss, "wick_sym", mes_session_low)

    # Hidden SMT: body/close-based divergence (fires only when wick SMT did not).
    # MES close makes new session extreme but MNQ close does not confirm.
    if HIDDEN_SMT_ENABLED:
        if _cached is not None:
            mes_close_session_high = _cached["mes_ch"]
            mnq_close_session_high = _cached["mnq_ch"]
            mes_close_session_low  = _cached["mes_cl"]
            mnq_close_session_low  = _cached["mnq_cl"]
        else:
            session_slice = slice(session_start_idx, bar_idx)
            mes_close_session_high = mes_bars["Close"].iloc[session_slice].max()
            mnq_close_session_high = mnq_bars["Close"].iloc[session_slice].max()
            mes_close_session_low  = mes_bars["Close"].iloc[session_slice].min()
            mnq_close_session_low  = mnq_bars["Close"].iloc[session_slice].min()
        if cur_mes["Close"] > mes_close_session_high and cur_mnq["Close"] <= mnq_close_session_high:
            smt_sweep = cur_mes["Close"] - mes_close_session_high
            mnq_miss   = mnq_close_session_high - cur_mnq["Close"]
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("short", smt_sweep, mnq_miss, "body", mnq_close_session_high)
        if cur_mes["Close"] < mes_close_session_low and cur_mnq["Close"] >= mnq_close_session_low:
            smt_sweep = mes_close_session_low - cur_mes["Close"]
            mnq_miss   = cur_mnq["Close"] - mnq_close_session_low
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("long", smt_sweep, mnq_miss, "body", mnq_close_session_low)
    return None


def find_entry_bar(
    mnq_bars: pd.DataFrame,
    direction: str,
    divergence_idx: int,
    session_end_idx: int,
) -> int | None:
    """Find the confirmation candle after a divergence signal.

    For "short": first bar after divergence_idx where:
        - close < open  (bearish bar)
        - high > close of most recent prior bullish bar (wick pierces bull body)

    For "long": first bar after divergence_idx where:
        - close > open  (bullish bar)
        - low < close of most recent prior bearish bar (wick pierces bear body)

    Returns bar index or None if no confirmation before session_end_idx.
    """
    if direction == "short":
        # Find the most recent bullish bar at or before divergence_idx
        last_bull_close = None
        for i in range(divergence_idx, -1, -1):
            bar = mnq_bars.iloc[i]
            if bar["Close"] > bar["Open"]:
                last_bull_close = bar["Close"]
                break
        if last_bull_close is None:
            return None
        # Confirmation: bearish bar whose wick pierces the bull body close
        for i in range(divergence_idx + 1, session_end_idx):
            bar = mnq_bars.iloc[i]
            if bar["Close"] < bar["Open"] and bar["High"] > last_bull_close:
                return i
    else:  # "long"
        # Find the most recent bearish bar at or before divergence_idx
        last_bear_close = None
        for i in range(divergence_idx, -1, -1):
            bar = mnq_bars.iloc[i]
            if bar["Close"] < bar["Open"]:
                last_bear_close = bar["Close"]
                break
        if last_bear_close is None:
            return None
        # Confirmation: bullish bar whose wick pierces the bear body close
        for i in range(divergence_idx + 1, session_end_idx):
            bar = mnq_bars.iloc[i]
            if bar["Close"] > bar["Open"] and bar["Low"] < last_bear_close:
                return i
    return None


def compute_tdo(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """Return True Day Open = opening price of the 9:30 AM ET bar for given date.

    Falls back to the first available bar on that date if 9:30 bar is absent
    (e.g., for signals detected before 9:30 AM in the 9:00–9:30 window).
    Returns None if no bars exist for the date.
    """
    target_time = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")
    if target_time in mnq_bars.index:
        return float(mnq_bars.loc[target_time, "Open"])
    # Proxy: use the first available bar on that date
    day_bars = mnq_bars[mnq_bars.index.date == date]
    if day_bars.empty:
        return None
    return float(day_bars.iloc[0]["Open"])


def compute_midnight_open(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """Return the Open of the first 1m/5m bar at or after 00:00 ET on date.

    ICT canonical intraday reversion target. Falls back to the first bar on
    that date if no bar exists exactly at midnight (e.g. on 5m resampled data).
    Returns None if no bars exist for the date.
    """
    day_bars = mnq_bars[mnq_bars.index.date == date]
    if day_bars.empty:
        return None
    midnight = pd.Timestamp(f"{date} 00:00:00", tz="America/New_York")
    after_midnight = day_bars[day_bars.index >= midnight]
    if not after_midnight.empty:
        return float(after_midnight.iloc[0]["Open"])
    return float(day_bars.iloc[0]["Open"])


def compute_overnight_range(mnq_bars: pd.DataFrame, date: datetime.date) -> dict:
    """Return overnight high/low: bars on date with time < 09:00 ET.

    Returns {"overnight_high": float, "overnight_low": float} or
    {"overnight_high": None, "overnight_low": None} if no pre-9am bars exist.
    """
    mask = (
        (mnq_bars.index.date == date) &
        (mnq_bars.index.time < pd.Timestamp("2000-01-01 09:00:00").time())
    )
    bars = mnq_bars[mask]
    if bars.empty:
        return {"overnight_high": None, "overnight_low": None}
    return {
        "overnight_high": float(bars["High"].max()),
        "overnight_low":  float(bars["Low"].min()),
    }


def detect_fvg(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
    lookback: int = 20,
) -> dict | None:
    """Find the most recent Fair Value Gap in the given direction before bar_idx.

    Bullish FVG (long): bar3.Low > bar1.High — gap on the way up.
      Zone = [bar1.High, bar3.Low]; price retraces down into this zone for Layer B.
    Bearish FVG (short): bar3.High < bar1.Low — gap on the way down.
      Zone = [bar3.High, bar1.Low]; price retraces up into this zone for Layer B.

    Returns {"fvg_high": float, "fvg_low": float, "fvg_bar": int} or None.
    Always None when FVG_ENABLED is False.
    """
    if not FVG_ENABLED:
        return None
    # bar3 = i+2 must be strictly < bar_idx → i <= bar_idx-3
    if bar_idx < 3:
        return None
    start = max(0, bar_idx - lookback)
    if start > bar_idx - 3:
        return None
    # Pre-extract arrays once — avoids 4× .iloc overhead per iteration
    highs = bars["High"].values
    lows  = bars["Low"].values
    for i in range(bar_idx - 3, start - 1, -1):
        bar1_h = highs[i]
        bar1_l = lows[i]
        bar3_h = highs[i + 2]
        bar3_l = lows[i + 2]
        if direction == "long":
            if bar3_l > bar1_h and (bar3_l - bar1_h) >= FVG_MIN_SIZE_PTS:
                return {"fvg_high": bar3_l, "fvg_low": bar1_h, "fvg_bar": i}
        else:  # short
            if bar3_h < bar1_l and (bar1_l - bar3_h) >= FVG_MIN_SIZE_PTS:
                return {"fvg_high": bar1_l, "fvg_low": bar3_h, "fvg_bar": i}
    return None


def detect_displacement(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
) -> bool:
    """True if bar_idx is a displacement candle in the given direction.

    Displacement: body (|Close - Open|) >= MIN_DISPLACEMENT_PTS and candle direction
    matches. Always False when SMT_OPTIONAL is False or MIN_DISPLACEMENT_PTS <= 0.
    """
    if not SMT_OPTIONAL or MIN_DISPLACEMENT_PTS <= 0:
        return False
    bar = bars.iloc[bar_idx]
    body = abs(float(bar["Close"]) - float(bar["Open"]))
    if body < MIN_DISPLACEMENT_PTS:
        return False
    # This check is only reachable when MIN_DISPLACEMENT_BODY_PTS > MIN_DISPLACEMENT_PTS;
    # if body_pts ≤ displacement_pts the earlier check already returned False.
    if MIN_DISPLACEMENT_BODY_PTS > 0 and body < MIN_DISPLACEMENT_BODY_PTS:
        return False
    if direction == "long"  and bar["Close"] > bar["Open"]: return True
    if direction == "short" and bar["Close"] < bar["Open"]: return True
    return False


def detect_smt_fill(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    lookback: int = 20,
) -> tuple | None:
    """Detect inter-instrument FVG fill divergence.

    Bearish: current MES bar reaches into a recent bearish FVG that MNQ has not reached.
    Bullish: current MNQ bar reaches into a recent bullish FVG that MES has not reached.

    Returns (direction, fvg_high, fvg_low) or None.
    Always None when SMT_FILL_ENABLED is False.
    """
    if not SMT_FILL_ENABLED:
        return None
    mes_bar = mes_bars.iloc[bar_idx]
    mnq_bar = mnq_bars.iloc[bar_idx]
    # Bearish fill: MES rallies into a bearish FVG; MNQ has not reached the zone
    mes_fvg = detect_fvg(mes_bars, bar_idx, "short", lookback)
    if mes_fvg is not None:
        if float(mes_bar["High"]) >= mes_fvg["fvg_low"] and float(mnq_bar["High"]) < mes_fvg["fvg_low"]:
            return ("short", mes_fvg["fvg_high"], mes_fvg["fvg_low"])
    # Bullish fill: MNQ retraces into a bullish FVG; MES has not reached the zone
    mnq_fvg = detect_fvg(mnq_bars, bar_idx, "long", lookback)
    if mnq_fvg is not None:
        if float(mnq_bar["Low"]) <= mnq_fvg["fvg_high"] and float(mes_bar["Low"]) > mnq_fvg["fvg_high"]:
            return ("long", mnq_fvg["fvg_high"], mnq_fvg["fvg_low"])
    return None


def print_direction_breakdown(stats: dict, prefix: str = "") -> None:
    """Print per-direction trade count, win rate, avg PnL, and exit breakdown.

    Uses the same {prefix}{key}: {value} format as print_results so autoresearch
    agents can parse direction metrics alongside the standard fold output.

    Reads from stats["trade_records"]. Prints nothing if trade_records is absent
    or empty. Controlled by PRINT_DIRECTION_BREAKDOWN constant (caller's responsibility
    to check before calling).

    Args:
        stats:  Dict returned by run_backtest or _compute_metrics.
        prefix: String prepended to every printed key (e.g. "fold1_train_").
    """
    trades = stats.get("trade_records", [])
    if not trades:
        return
    for direction in ("long", "short"):
        subset = [t for t in trades if t["direction"] == direction]
        n = len(subset)
        wins = sum(1 for t in subset if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in subset)
        win_rate  = round(wins / n, 4) if n > 0 else 0.0
        avg_pnl   = round(total_pnl / n, 2) if n > 0 else 0.0
        print(f"{prefix}{direction}_trades: {n}")
        print(f"{prefix}{direction}_win_rate: {win_rate}")
        print(f"{prefix}{direction}_avg_pnl: {avg_pnl}")
        exits: dict[str, int] = {}
        for t in subset:
            exits[t["exit_type"]] = exits.get(t["exit_type"], 0) + 1
        for exit_type, count in exits.items():
            print(f"{prefix}{direction}_exit_{exit_type}: {count}")


def find_anchor_close(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
) -> float | None:
    """Return the close of the most recent opposite-direction bar at or before bar_idx.

    For "short" setups: looks backward for the most recent bullish bar (close > open).
    For "long"  setups: looks backward for the most recent bearish bar (close < open).

    Returns None if no qualifying bar exists before bar_idx.
    The result is stored as `anchor_close` in the pending-signal state — it is the
    reference price that a confirmation bar must pierce.
    """
    for i in range(bar_idx, -1, -1):
        bar = bars.iloc[i]
        if direction == "short" and bar["Close"] > bar["Open"]:
            return float(bar["Close"])
        if direction == "long" and bar["Close"] < bar["Open"]:
            return float(bar["Close"])
    return None


def is_confirmation_bar(
    bar: pd.Series,
    anchor_close: float,
    direction: str,
) -> bool:
    """Return True if `bar` qualifies as a signal confirmation candle.

    For "short": bar is bearish (close < open) AND high > anchor_close.
    For "long":  bar is bullish (close > open) AND low  < anchor_close.

    This is a single-bar check — the caller iterates bars and calls this each time.
    Replaces the forward scan loop in find_entry_bar().
    """
    if direction == "short":
        return bar["Close"] < bar["Open"] and bar["High"] > anchor_close
    else:  # "long"
        return bar["Close"] > bar["Open"] and bar["Low"] < anchor_close


def _quarterly_session_windows(date: "datetime.date") -> list:
    """Return 4×6h quarterly session boundary tuples (start, end) as ET Timestamps for date.

    Windows: Asia 18:00–00:00, London 00:00–06:00, NY Morning 06:00–12:00, NY Evening 12:00–18:00.
    Asia window straddles midnight: start is prev calendar day 18:00, end is date 00:00.
    """
    tz = "America/New_York"
    prev = date - datetime.timedelta(days=1)
    return [
        (pd.Timestamp(f"{prev} 18:00", tz=tz), pd.Timestamp(f"{date} 00:00", tz=tz)),
        (pd.Timestamp(f"{date} 00:00",  tz=tz), pd.Timestamp(f"{date} 06:00", tz=tz)),
        (pd.Timestamp(f"{date} 06:00",  tz=tz), pd.Timestamp(f"{date} 12:00", tz=tz)),
        (pd.Timestamp(f"{date} 12:00",  tz=tz), pd.Timestamp(f"{date} 18:00", tz=tz)),
    ]


def _compute_ref_levels(
    prev_day_mnq: "pd.DataFrame | None",
    prev_day_mes: "pd.DataFrame | None",
    prev_session_mnq: "pd.DataFrame | None",
    prev_session_mes: "pd.DataFrame | None",
    week_mnq: "pd.DataFrame | None",
    week_mes: "pd.DataFrame | None",
) -> dict:
    """Pre-compute reference level cache from prior-day, prior-session, and week slices.

    Returns a dict with high/low keys for each reference window (wick-based and close-based).
    None values indicate unavailable data; callers skip the corresponding check.
    """
    def _hl(df, col_h="High", col_l="Low"):
        if df is None or df.empty:
            return None, None
        return float(df[col_h].max()), float(df[col_l].min())

    mnq_pd_h, mnq_pd_l = _hl(prev_day_mnq)
    mes_pd_h, mes_pd_l = _hl(prev_day_mes)
    mnq_ps_h, mnq_ps_l = _hl(prev_session_mnq)
    mes_ps_h, mes_ps_l = _hl(prev_session_mes)
    mnq_wk_h, mnq_wk_l = _hl(week_mnq)
    mes_wk_h, mes_wk_l = _hl(week_mes)
    # Close-based equivalents for hidden SMT
    mnq_pd_ch, mnq_pd_cl = _hl(prev_day_mnq, "Close", "Close")
    mes_pd_ch, mes_pd_cl = _hl(prev_day_mes, "Close", "Close")
    mnq_ps_ch, mnq_ps_cl = _hl(prev_session_mnq, "Close", "Close")
    mes_ps_ch, mes_ps_cl = _hl(prev_session_mes, "Close", "Close")
    mnq_wk_ch, mnq_wk_cl = _hl(week_mnq, "Close", "Close")
    mes_wk_ch, mes_wk_cl = _hl(week_mes, "Close", "Close")
    return {
        "prev_day_mnq_high": mnq_pd_h,   "prev_day_mnq_low": mnq_pd_l,
        "prev_day_mes_high": mes_pd_h,   "prev_day_mes_low": mes_pd_l,
        "prev_session_mnq_high": mnq_ps_h, "prev_session_mnq_low": mnq_ps_l,
        "prev_session_mes_high": mes_ps_h, "prev_session_mes_low": mes_ps_l,
        "week_mnq_high": mnq_wk_h,       "week_mnq_low": mnq_wk_l,
        "week_mes_high": mes_wk_h,       "week_mes_low": mes_wk_l,
        # Close-based for hidden SMT
        "prev_day_mnq_close_high": mnq_pd_ch,   "prev_day_mnq_close_low": mnq_pd_cl,
        "prev_day_mes_close_high": mes_pd_ch,   "prev_day_mes_close_low": mes_pd_cl,
        "prev_session_mnq_close_high": mnq_ps_ch, "prev_session_mnq_close_low": mnq_ps_cl,
        "prev_session_mes_close_high": mes_ps_ch, "prev_session_mes_close_low": mes_ps_cl,
        "week_mnq_close_high": mnq_wk_ch, "week_mnq_close_low": mnq_wk_cl,
        "week_mes_close_high": mes_wk_ch, "week_mes_close_low": mes_wk_cl,
    }


def _check_smt_against_ref(
    cur_mes, cur_mnq,
    ref_mes_high, ref_mes_low,
    ref_mnq_high, ref_mnq_low,
    use_close: bool = False,
) -> "tuple[str, float, float, str, float] | None":
    """Check MES-leads SMT divergence against a single reference level pair.

    Returns (direction, sweep_pts, miss_pts, smt_type, smt_defended_level) or None.
    use_close=True uses Close values (hidden SMT body/close check).
    """
    if ref_mes_high is None:
        return None
    if use_close:
        cur_mes_h = float(cur_mes["Close"])
        cur_mes_l = float(cur_mes["Close"])
        cur_mnq_h = float(cur_mnq["Close"])
        cur_mnq_l = float(cur_mnq["Close"])
        smt_tag = "body"
    else:
        cur_mes_h = float(cur_mes["High"])
        cur_mes_l = float(cur_mes["Low"])
        cur_mnq_h = float(cur_mnq["High"])
        cur_mnq_l = float(cur_mnq["Low"])
        smt_tag = "wick"
    if ref_mnq_high is not None and cur_mes_h > ref_mes_high and cur_mnq_h <= ref_mnq_high:
        sweep = cur_mes_h - ref_mes_high
        miss  = ref_mnq_high - cur_mnq_h
        if MIN_SMT_SWEEP_PTS > 0 and sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and miss < MIN_SMT_MISS_PTS:
            return None
        return ("short", sweep, miss, smt_tag, ref_mnq_high)
    if ref_mnq_low is not None and cur_mes_l < ref_mes_low and cur_mnq_l >= ref_mnq_low:
        sweep = ref_mes_low - cur_mes_l
        miss  = cur_mnq_l - ref_mnq_low
        if MIN_SMT_SWEEP_PTS > 0 and sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and miss < MIN_SMT_MISS_PTS:
            return None
        return ("long", sweep, miss, smt_tag, ref_mnq_low)
    return None


def screen_session(
    mnq_bars: pd.DataFrame,
    mes_bars: pd.DataFrame,
    tdo: float,
    midnight_open=None,
    overnight_range=None,
    prev_day_mes: "pd.DataFrame | None" = None,
    prev_day_mnq: "pd.DataFrame | None" = None,
    prev_session_mes: "pd.DataFrame | None" = None,
    prev_session_mnq: "pd.DataFrame | None" = None,
) -> dict | None:
    """Session signal scanner — compatibility shim for signal_smt.py live trading.

    Implements the same scan as the old screen_session using the new helpers
    (find_anchor_close, is_confirmation_bar, _build_signal_from_bar) so that the
    live trading module (signal_smt.py) continues to work unchanged.

    For backtesting use run_backtest() which runs the full bar-by-bar state machine.
    """
    if mnq_bars.empty or mes_bars.empty:
        return None
    if tdo is None or tdo == 0.0:
        return None

    n_bars = min(len(mnq_bars), len(mes_bars))
    min_signal_ts = mnq_bars.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)
    mes_reset = mes_bars.reset_index(drop=True)
    mnq_reset = mnq_bars.reset_index(drop=True)
    import math as _math
    _mes_h_arr  = mes_reset["High"].values
    _mes_l_arr  = mes_reset["Low"].values
    _mnq_h_arr  = mnq_reset["High"].values
    _mnq_l_arr  = mnq_reset["Low"].values
    _mes_c_arr  = mes_reset["Close"].values
    _mnq_c_arr  = mnq_reset["Close"].values
    _ses_mes_h  = _ses_mes_l  = float("nan")
    _ses_mnq_h  = _ses_mnq_l  = float("nan")
    _ses_mes_ch = _ses_mes_cl = float("nan")
    _ses_mnq_ch = _ses_mnq_cl = float("nan")

    # Pre-compute reference levels for EXPANDED_REFERENCE_LEVELS check
    _ref_lvls = {}
    if EXPANDED_REFERENCE_LEVELS:
        _ref_lvls = _compute_ref_levels(
            prev_day_mnq, prev_day_mes, prev_session_mnq, prev_session_mes, None, None
        )

    # HTF state: per-period running extremes for HTF_VISIBILITY_REQUIRED
    # Each entry: (period_start_ns, cur_mes_h, cur_mes_l, cur_mnq_h, cur_mnq_l,
    #              cur_mes_ch, cur_mes_cl, cur_mnq_ch, cur_mnq_cl,
    #              prev_mes_h, prev_mes_l, prev_mnq_h, prev_mnq_l,
    #              prev_mes_ch, prev_mes_cl, prev_mnq_ch, prev_mnq_cl)
    _htf_state: dict = {}
    if HTF_VISIBILITY_REQUIRED:
        for _T in HTF_PERIODS_MINUTES:
            _htf_state[_T] = {
                "pstart": None,
                "c_mes_h": float("nan"), "c_mes_l": float("nan"),
                "c_mnq_h": float("nan"), "c_mnq_l": float("nan"),
                "c_mes_ch": float("nan"), "c_mes_cl": float("nan"),
                "c_mnq_ch": float("nan"), "c_mnq_cl": float("nan"),
                "p_mes_h": None, "p_mes_l": None,
                "p_mnq_h": None, "p_mnq_l": None,
                "p_mes_ch": None, "p_mes_cl": None,
                "p_mnq_ch": None, "p_mnq_cl": None,
            }

    for bar_idx in range(n_bars):
        # Update running extremes with previous bar — done at TOP so continue-statements
        # elsewhere in the loop body cannot skip the update.
        if bar_idx > 0:
            _p = bar_idx - 1
            _v = float(_mes_h_arr[_p])
            _ses_mes_h  = _v if _math.isnan(_ses_mes_h)  else max(_ses_mes_h,  _v)
            _v = float(_mes_l_arr[_p])
            _ses_mes_l  = _v if _math.isnan(_ses_mes_l)  else min(_ses_mes_l,  _v)
            _v = float(_mnq_h_arr[_p])
            _ses_mnq_h  = _v if _math.isnan(_ses_mnq_h)  else max(_ses_mnq_h,  _v)
            _v = float(_mnq_l_arr[_p])
            _ses_mnq_l  = _v if _math.isnan(_ses_mnq_l)  else min(_ses_mnq_l,  _v)
            _v = float(_mes_c_arr[_p])
            _ses_mes_ch = _v if _math.isnan(_ses_mes_ch) else max(_ses_mes_ch, _v)
            _ses_mes_cl = _v if _math.isnan(_ses_mes_cl) else min(_ses_mes_cl, _v)
            _v = float(_mnq_c_arr[_p])
            _ses_mnq_ch = _v if _math.isnan(_ses_mnq_ch) else max(_ses_mnq_ch, _v)
            _ses_mnq_cl = _v if _math.isnan(_ses_mnq_cl) else min(_ses_mnq_cl, _v)

        # HTF period extreme update — include current bar in running extreme
        if HTF_VISIBILITY_REQUIRED:
            ts_bar = mnq_bars.index[bar_idx]
            ts_ns  = ts_bar.value  # nanoseconds since epoch
            for _T, _hs in _htf_state.items():
                _period_ns = _T * 60 * 10**9
                _pstart = (ts_ns // _period_ns) * _period_ns
                if _hs["pstart"] != _pstart:
                    # Period boundary crossed — snapshot prior period
                    if _hs["pstart"] is not None and not _math.isnan(_hs["c_mes_h"]):
                        _hs["p_mes_h"]  = _hs["c_mes_h"];  _hs["p_mes_l"]  = _hs["c_mes_l"]
                        _hs["p_mnq_h"]  = _hs["c_mnq_h"];  _hs["p_mnq_l"]  = _hs["c_mnq_l"]
                        _hs["p_mes_ch"] = _hs["c_mes_ch"]; _hs["p_mes_cl"] = _hs["c_mes_cl"]
                        _hs["p_mnq_ch"] = _hs["c_mnq_ch"]; _hs["p_mnq_cl"] = _hs["c_mnq_cl"]
                    _hs["pstart"] = _pstart
                    _hs["c_mes_h"] = float(_mes_h_arr[bar_idx])
                    _hs["c_mes_l"] = float(_mes_l_arr[bar_idx])
                    _hs["c_mnq_h"] = float(_mnq_h_arr[bar_idx])
                    _hs["c_mnq_l"] = float(_mnq_l_arr[bar_idx])
                    _hs["c_mes_ch"] = float(_mes_c_arr[bar_idx])
                    _hs["c_mes_cl"] = float(_mes_c_arr[bar_idx])
                    _hs["c_mnq_ch"] = float(_mnq_c_arr[bar_idx])
                    _hs["c_mnq_cl"] = float(_mnq_c_arr[bar_idx])
                else:
                    _hs["c_mes_h"]  = max(_hs["c_mes_h"],  float(_mes_h_arr[bar_idx]))
                    _hs["c_mes_l"]  = min(_hs["c_mes_l"],  float(_mes_l_arr[bar_idx]))
                    _hs["c_mnq_h"]  = max(_hs["c_mnq_h"],  float(_mnq_h_arr[bar_idx]))
                    _hs["c_mnq_l"]  = min(_hs["c_mnq_l"],  float(_mnq_l_arr[bar_idx]))
                    _hs["c_mes_ch"] = max(_hs["c_mes_ch"], float(_mes_c_arr[bar_idx]))
                    _hs["c_mes_cl"] = min(_hs["c_mes_cl"], float(_mes_c_arr[bar_idx]))
                    _hs["c_mnq_ch"] = max(_hs["c_mnq_ch"], float(_mnq_c_arr[bar_idx]))
                    _hs["c_mnq_cl"] = min(_hs["c_mnq_cl"], float(_mnq_c_arr[bar_idx]))

        _smt_cache = {
            "mes_h":  _ses_mes_h,  "mes_l":  _ses_mes_l,
            "mnq_h":  _ses_mnq_h,  "mnq_l":  _ses_mnq_l,
            "mes_ch": _ses_mes_ch, "mes_cl": _ses_mes_cl,
            "mnq_ch": _ses_mnq_ch, "mnq_cl": _ses_mnq_cl,
        }

        if mnq_bars.index[bar_idx] < min_signal_ts:
            continue

        _smt = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0, _cached=_smt_cache)

        # Expanded reference levels: try prev-day, prev-session, week extremes when flag enabled
        if _smt is None and EXPANDED_REFERENCE_LEVELS and _ref_lvls:
            _cur_mes = mes_reset.iloc[bar_idx]
            _cur_mnq = mnq_reset.iloc[bar_idx]
            _best_ref: "tuple | None" = None
            for _ref_key in (
                ("prev_day_mes_high", "prev_day_mes_low", "prev_day_mnq_high", "prev_day_mnq_low", False),
                ("prev_session_mes_high", "prev_session_mes_low", "prev_session_mnq_high", "prev_session_mnq_low", False),
                ("week_mes_high", "week_mes_low", "week_mnq_high", "week_mnq_low", False),
                ("prev_day_mes_close_high", "prev_day_mes_close_low", "prev_day_mnq_close_high", "prev_day_mnq_close_low", True),
                ("prev_session_mes_close_high", "prev_session_mes_close_low", "prev_session_mnq_close_high", "prev_session_mnq_close_low", True),
                ("week_mes_close_high", "week_mes_close_low", "week_mnq_close_high", "week_mnq_close_low", True),
            ):
                _mh, _ml, _nh, _nl, _use_close = _ref_key
                # Skip close-based ref levels unless HIDDEN_SMT_ENABLED
                if _use_close and not HIDDEN_SMT_ENABLED:
                    continue
                _candidate = _check_smt_against_ref(
                    _cur_mes, _cur_mnq,
                    _ref_lvls.get(_mh), _ref_lvls.get(_ml),
                    _ref_lvls.get(_nh), _ref_lvls.get(_nl),
                    use_close=_use_close,
                )
                if _candidate is not None:
                    # Pick the result with the largest sweep_pts
                    if _best_ref is None or _candidate[1] > _best_ref[1]:
                        _best_ref = _candidate
            if _best_ref is not None:
                _smt = _best_ref

        # SMT-optional: accept displacement if no wick/hidden SMT found
        _smt_fill = None
        _displacement_direction = None
        if _smt is None:
            if SMT_FILL_ENABLED:
                _smt_fill = detect_smt_fill(mes_reset, mnq_reset, bar_idx)
            if _smt_fill is None and SMT_OPTIONAL:
                for _d in ("short", "long"):
                    if detect_displacement(mnq_reset, bar_idx, _d):
                        _displacement_direction = _d
                        break
        if _smt is None and _smt_fill is None and _displacement_direction is None:
            continue

        # Resolve effective direction
        if _smt is not None:
            direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt
        elif _smt_fill is not None:
            direction, _fill_fvg_high, _fill_fvg_low = _smt_fill
            _smt_sweep = _smt_miss = 0.0; _smt_type = "fill"; _smt_defended = None
        else:
            direction = _displacement_direction
            _smt_sweep = _smt_miss = 0.0; _smt_type = "displacement"; _smt_defended = None

        if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
            continue

        # HTF visibility filter: skip if signal not visible on any configured HTF period
        if HTF_VISIBILITY_REQUIRED and _htf_state:
            _htf_confirmed: list = []
            for _T, _hs in _htf_state.items():
                if _hs["p_mes_h"] is None:
                    continue
                if direction == "short":
                    if _hs["c_mes_h"] > _hs["p_mes_h"] and _hs["c_mnq_h"] <= _hs["p_mnq_h"]:
                        _htf_confirmed.append(_T)
                    elif HIDDEN_SMT_ENABLED and _hs["c_mes_ch"] > _hs["p_mes_ch"] and _hs["c_mnq_ch"] <= _hs["p_mnq_ch"]:
                        _htf_confirmed.append(_T)
                else:
                    if _hs["c_mes_l"] < _hs["p_mes_l"] and _hs["c_mnq_l"] >= _hs["p_mnq_l"]:
                        _htf_confirmed.append(_T)
                    elif HIDDEN_SMT_ENABLED and _hs["c_mes_cl"] < _hs["p_mes_cl"] and _hs["c_mnq_cl"] >= _hs["p_mnq_cl"]:
                        _htf_confirmed.append(_T)
            if not _htf_confirmed:
                continue
            # Store confirmed timeframes for later insertion into signal dict
            _htf_confirmed_tfs = _htf_confirmed
        else:
            _htf_confirmed_tfs = None

        # Overnight sweep gate: require overnight H (shorts) or L (longs) to have been swept.
        if OVERNIGHT_SWEEP_REQUIRED and overnight_range is not None:
            oh = overnight_range.get("overnight_high")
            ol = overnight_range.get("overnight_low")
            if direction == "short" and oh is not None:
                pre_session_high = mnq_reset["High"].iloc[:bar_idx].max() if bar_idx > 0 else 0
                if pre_session_high <= oh:
                    continue
            if direction == "long" and ol is not None:
                pre_session_low = mnq_reset["Low"].iloc[:bar_idx].min() if bar_idx > 0 else float("inf")
                if pre_session_low >= ol:
                    continue

        # Silver bullet window: only accept divergences during 09:50–10:10 ET.
        if SILVER_BULLET_WINDOW_ONLY:
            bar_t = mnq_bars.index[bar_idx].strftime("%H:%M")
            if not (SILVER_BULLET_START <= bar_t < SILVER_BULLET_END):
                continue

        _div_bar = mnq_reset.iloc[bar_idx]
        _div_bar_high = float(_div_bar["High"])
        _div_bar_low  = float(_div_bar["Low"])

        # Compute FVG zone after direction is resolved
        _fvg_zone = detect_fvg(mnq_reset, bar_idx, direction)

        # TP target selection: overnight range → midnight open → TDO (fallback).
        if OVERNIGHT_RANGE_AS_TP and overnight_range is not None:
            _raw = overnight_range.get("overnight_low" if direction == "short" else "overnight_high")
            _tp = _raw if _raw is not None else tdo
        elif MIDNIGHT_OPEN_AS_TP and midnight_open is not None:
            _tp = midnight_open
        else:
            _tp = tdo

        ac = find_anchor_close(mnq_reset, bar_idx, direction)
        if ac is None:
            continue

        # Scan forward for first confirmation bar after divergence.
        # ALWAYS_REQUIRE_CONFIRMATION: bar must also break the displacement bar's body boundary.
        _div_body_high = max(float(_div_bar["Open"]), float(_div_bar["Close"]))
        _div_body_low  = min(float(_div_bar["Open"]), float(_div_bar["Close"]))
        for conf_idx in range(bar_idx + 1, n_bars):
            conf_bar = mnq_reset.iloc[conf_idx]
            if not is_confirmation_bar(conf_bar, ac, direction):
                continue
            if ALWAYS_REQUIRE_CONFIRMATION:
                if direction == "short" and float(conf_bar["Close"]) >= _div_body_low:
                    continue
                if direction == "long" and float(conf_bar["Close"]) <= _div_body_high:
                    continue
            entry_time = mnq_bars.index[conf_idx]
            if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
                t = entry_time.strftime("%H:%M")
                if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                    break
            signal = _build_signal_from_bar(
                conf_bar, entry_time, direction, _tp,
                smt_sweep_pts=_smt_sweep,
                smt_miss_pts=_smt_miss,
                divergence_bar_high=_div_bar_high,
                divergence_bar_low=_div_bar_low,
                midnight_open=midnight_open,
                smt_defended_level=_smt_defended,
                smt_type=_smt_type,
                fvg_zone=_fvg_zone,
            )
            if signal is None:
                break
            signal["divergence_bar"] = bar_idx
            signal["entry_bar"] = conf_idx
            if HTF_VISIBILITY_REQUIRED:
                signal["htf_confirmed_timeframes"] = _htf_confirmed_tfs
            return signal

    return None


def _build_signal_from_bar(
    bar: pd.Series,
    ts: "pd.Timestamp",
    direction: str,
    tdo: float,
    smt_sweep_pts: float = 0.0,
    smt_miss_pts: float = 0.0,
    divergence_bar_idx: int = -1,
    # New Plan 1 fields:
    divergence_bar_high=None,
    divergence_bar_low=None,
    midnight_open=None,
    smt_defended_level=None,
    smt_type: str = "wick",
    # New Plan 2 fields:
    fvg_zone=None,
) -> dict | None:
    """Build a signal dict from a confirmed entry bar, applying all validity guards.

    Returns None if the signal fails TDO_VALIDITY_CHECK, MIN_STOP_POINTS,
    MIN_TDO_DISTANCE_PTS, or MAX_TDO_DISTANCE_PTS guards.
    """
    entry_price = float(bar["Close"])

    if TDO_VALIDITY_CHECK:
        if direction == "long" and tdo <= entry_price:
            return None
        if direction == "short" and tdo >= entry_price:
            return None

    distance_to_tdo = abs(entry_price - tdo)
    if MIN_TDO_DISTANCE_PTS > 0 and distance_to_tdo < MIN_TDO_DISTANCE_PTS:
        return None
    # Ceiling filter — trades with extreme TDO distance have collapsing RR and negative EP
    if MAX_TDO_DISTANCE_PTS < 999.0 and distance_to_tdo > MAX_TDO_DISTANCE_PTS:
        return None

    # Structural stop: beyond the divergence bar's wick extreme + buffer.
    # Falls back to ratio-based stop if divergence bar extremes are unavailable.
    if STRUCTURAL_STOP_MODE and divergence_bar_high is not None and divergence_bar_low is not None:
        if direction == "short":
            stop_price = divergence_bar_high + STRUCTURAL_STOP_BUFFER_PTS
        else:
            stop_price = divergence_bar_low - STRUCTURAL_STOP_BUFFER_PTS
    else:
        stop_ratio = SHORT_STOP_RATIO if direction == "short" else LONG_STOP_RATIO
        if direction == "short":
            stop_price = entry_price + stop_ratio * distance_to_tdo
        else:
            stop_price = entry_price - stop_ratio * distance_to_tdo

    # Solution C: reject inverted stops — stop on wrong side of entry guarantees bar-1 exit
    if direction == "long"  and stop_price >= entry_price:
        return None
    if direction == "short" and stop_price <= entry_price:
        return None

    if MIN_STOP_POINTS > 0 and abs(entry_price - stop_price) < MIN_STOP_POINTS:
        return None

    body = abs(float(bar["Close"]) - float(bar["Open"]))
    if MIN_DISPLACEMENT_BODY_PTS > 0 and body < MIN_DISPLACEMENT_BODY_PTS:
        return None

    bar_range = bar["High"] - bar["Low"]
    entry_bar_body_ratio = (
        abs(bar["Close"] - bar["Open"]) / bar_range if bar_range > 0 else 0.0
    )

    return {
        "direction":            direction,
        "entry_price":          entry_price,
        "entry_time":           ts,
        "take_profit":          tdo,
        "stop_price":           round(stop_price, 4),
        "tdo":                  tdo,
        "divergence_bar":       divergence_bar_idx,
        "entry_bar":            -1,
        # Diagnostic fields — captured for analysis, no filter logic applied
        "smt_sweep_pts":        round(smt_sweep_pts, 4),
        "smt_miss_pts":         round(smt_miss_pts, 4),
        "entry_bar_body_ratio": round(entry_bar_body_ratio, 4),
        # Plan 1 structural fields
        "divergence_bar_high":  divergence_bar_high,
        "divergence_bar_low":   divergence_bar_low,
        "midnight_open":        midnight_open,
        "smt_defended_level":   smt_defended_level,
        "smt_type":             smt_type,
        # Plan 2 fields
        "fvg_high":             float(fvg_zone["fvg_high"]) if fvg_zone else None,
        "fvg_low":              float(fvg_zone["fvg_low"])  if fvg_zone else None,
        "partial_exit_level":   round(entry_price + (tdo - entry_price) * PARTIAL_EXIT_LEVEL_RATIO, 4) if PARTIAL_EXIT_ENABLED else None,
        # Plan 4 diagnostic — always recorded
        "displacement_body_pts": round(body, 2),
    }



def manage_position(
    position: dict,
    current_bar: pd.Series,
) -> str:
    """Check exit conditions for an open position against one bar.

    Returns one of: "hold" | "exit_tp" | "exit_stop" | "exit_time"

    For longs:  stop hit if low  <= stop_price; TP hit if high >= take_profit
    For shorts: stop hit if high >= stop_price; TP hit if low  <= take_profit
    Exit-time is handled by the harness (not this function).

    Breakeven/trailing stop:
        If BREAKEVEN_TRIGGER_PCT > 0 and the favorable move expressed as a
        fraction of |entry − TDO| reaches the threshold, stop_price is moved
        to entry_price (breakeven). Mutations are applied directly to the
        position dict so subsequent bars use the updated stop level. Stop only
        ever tightens, never widens.

    Trail-after-TP:
        If TRAIL_AFTER_TP_PTS > 0, exit_tp is suppressed when TDO is first
        crossed; instead the stop trails TRAIL_AFTER_TP_PTS points behind the
        best post-TDO price, letting profits run further.
    """
    direction   = position["direction"]
    entry_price = position["entry_price"]
    tp          = position["take_profit"]

    # ── Trail-after-TP: stay in trade past TDO, trail stop behind best price ──
    if TRAIL_AFTER_TP_PTS > 0:
        if position.get("tp_breached"):
            # Already past TDO — update trailing stop each bar
            if direction == "short":
                best = min(position.get("best_after_tp", tp), current_bar["Low"])
                position["best_after_tp"] = best
                position["stop_price"]    = best + TRAIL_AFTER_TP_PTS
            else:
                best = max(position.get("best_after_tp", tp), current_bar["High"])
                position["best_after_tp"] = best
                position["stop_price"]    = best - TRAIL_AFTER_TP_PTS
        else:
            # Check if TDO was crossed this bar for the first time
            crossed = (direction == "short" and current_bar["Low"]  <= tp) or \
                      (direction == "long"  and current_bar["High"] >= tp)
            if crossed:
                position["tp_breached"] = True
                if direction == "short":
                    position["best_after_tp"] = min(tp, current_bar["Low"])
                    position["stop_price"]    = position["best_after_tp"] + TRAIL_AFTER_TP_PTS
                else:
                    position["best_after_tp"] = max(tp, current_bar["High"])
                    position["stop_price"]    = position["best_after_tp"] - TRAIL_AFTER_TP_PTS
                return "hold"

    # ── Layer B entry (FVG retracement) ─────────────────────────────────────
    if TWO_LAYER_POSITION and FVG_LAYER_B_TRIGGER:
        if not position.get("layer_b_entered") and not position.get("partial_done"):
            fvg_high = position.get("fvg_high")
            fvg_low  = position.get("fvg_low")
            if fvg_high is not None and fvg_low is not None:
                in_fvg = (
                    (direction == "long"  and current_bar["Low"]  <= fvg_high and current_bar["Low"]  >= fvg_low) or
                    (direction == "short" and current_bar["High"] >= fvg_low  and current_bar["High"] <= fvg_high)
                )
                if in_fvg:
                    total_target = position.get("total_contracts_target", position["contracts"])
                    layer_b_contracts = total_target - position["contracts"]
                    if layer_b_contracts > 0:
                        lb_entry = float(current_bar["Close"])
                        n_a = position["contracts"]
                        position["entry_price"] = (
                            position["entry_price"] * n_a + lb_entry * layer_b_contracts
                        ) / (n_a + layer_b_contracts)
                        position["contracts"]           += layer_b_contracts
                        position["layer_b_entered"]      = True
                        position["layer_b_entry_price"]  = lb_entry
                        position["layer_b_contracts"]    = layer_b_contracts
                        # Tighten stop to FVG boundary on Layer B entry
                        if direction == "long":
                            new_stop = fvg_low - STRUCTURAL_STOP_BUFFER_PTS
                            position["stop_price"] = max(position["stop_price"], new_stop)
                        else:
                            new_stop = fvg_high + STRUCTURAL_STOP_BUFFER_PTS
                            position["stop_price"] = min(position["stop_price"], new_stop)

    # ── Breakeven / trailing stop update ─────────────────────────────────────
    # Skip breakeven management once we are trailing past TDO (stop is already ahead of entry)
    if BREAKEVEN_TRIGGER_PCT > 0 and not position.get("tp_breached"):
        tdo_dist = abs(entry_price - tp)
        if tdo_dist > 0:
            if direction == "short":
                progress = (entry_price - current_bar["Low"]) / tdo_dist
            else:
                progress = (current_bar["High"] - entry_price) / tdo_dist
            if progress >= BREAKEVEN_TRIGGER_PCT:
                # Only tighten the stop, never widen it
                if direction == "short":
                    position["stop_price"] = min(position["stop_price"], entry_price)
                else:
                    position["stop_price"] = max(position["stop_price"], entry_price)
                position["breakeven_active"] = True

    # ── Thesis-invalidation exits (close-based; fire before stop check) ─────────
    # MSS: divergence extreme breached on a closing basis.
    if INVALIDATION_MSS_EXIT:
        div_low  = position.get("divergence_bar_low")
        div_high = position.get("divergence_bar_high")
        if direction == "long" and div_low is not None and current_bar["Close"] < div_low:
            return "exit_invalidation_mss"
        if direction == "short" and div_high is not None and current_bar["Close"] > div_high:
            return "exit_invalidation_mss"

    # CISD: midnight open breached on a closing basis.
    if INVALIDATION_CISD_EXIT:
        mo = position.get("midnight_open")
        if mo is not None:
            if direction == "long"  and current_bar["Close"] < mo:
                return "exit_invalidation_cisd"
            if direction == "short" and current_bar["Close"] > mo:
                return "exit_invalidation_cisd"

    # SMT: MNQ defended level breached on a closing basis.
    if INVALIDATION_SMT_EXIT:
        defended = position.get("smt_defended_level")
        if defended is not None:
            if direction == "long"  and current_bar["Close"] < defended:
                return "exit_invalidation_smt"
            if direction == "short" and current_bar["Close"] > defended:
                return "exit_invalidation_smt"

    stop = position["stop_price"]

    # ── Partial exit at first draw on liquidity ─────────────────────────────
    if PARTIAL_EXIT_ENABLED and not position.get("partial_done"):
        lvl = position.get("partial_exit_level")
        if lvl is not None:
            if direction == "long"  and current_bar["High"] >= lvl:
                position["partial_done"]  = True
                position["partial_price"] = lvl
                return "partial_exit"
            if direction == "short" and current_bar["Low"]  <= lvl:
                position["partial_done"]  = True
                position["partial_price"] = lvl
                return "partial_exit"

    # ── Exit checks ───────────────────────────────────────────────────────────
    # exit_tp is only used when trail-after-TP is disabled; otherwise the stop
    # takes over once TDO is breached (handled in the block above).
    if direction == "long":
        if current_bar["Low"]  <= stop:                            return "exit_stop"
        if TRAIL_AFTER_TP_PTS == 0 and current_bar["High"] >= tp: return "exit_tp"
    else:  # short
        if current_bar["High"] >= stop:                            return "exit_stop"
        if TRAIL_AFTER_TP_PTS == 0 and current_bar["Low"]  <= tp: return "exit_tp"
    return "hold"
