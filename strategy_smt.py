"""strategy_smt.py — SMT Divergence strategy constants and functions. Fully mutable — owned by the optimizing agent."""
import datetime
import json
import math as _math
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

# TDO anchor choice: False = 9:30 RTH open (legacy, the code's "TDO"); True = 00:00 ET
# Midnight Open (ICT-canonical bias/reversion reference). Flipping changes the reference
# used everywhere — target, stop (|entry-TDO|), validity check, partial-exit level, trail.
# Separate from MIDNIGHT_OPEN_AS_TP which only swaps the TP target.
TDO_USE_MIDNIGHT: bool = True

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
LONG_STOP_RATIO  = 0.40
SHORT_STOP_RATIO = 0.40

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
# Optimizer search space: [25.0, 50.0, 75.0, 100.0].
TRAIL_AFTER_TP_PTS = 25.0

# TRAIL_ACTIVATION_R: trail only takes over after price travels this many R-multiples
# of |entry - stop| past TDO. 0.0 = activate immediately at TDO (legacy behaviour).
# Prevents a bare TDO tick-through from enabling a 50-pt adverse stop placement.
# R-multiple is scale-invariant, consistent with BREAKEVEN_TRIGGER_PCT.
# Optimizer search space: [0.0, 0.5, 1.0, 1.5, 2.0]
TRAIL_ACTIVATION_R: float = 1.0

# Maximum TDO distance filter: skip signals where |entry - TDO| > this value in MNQ pts.
# Cross-tab finding: TDO<20 has WR=37-43% and EP=$32-$59 across ALL re-entry sequences,
# including 5th+. TDO>100 trades are structurally losing (EP=−$2.04). TDO>50 barely break
# even. The quality degradation at high re-entry counts is driven by TDO distance, not depth.
# Optimizer search space: [15, 20, 25, 30, 40, 999].
# Set 999.0 to disable (pass-through for all distances).
MAX_TDO_DISTANCE_PTS = 40.0

# Maximum re-entries per session day.
# At TDO<20 (with MAX_TDO_DISTANCE_PTS applied), even Seq#5+ has EP=$32, so this filter
# is less important than expected. Most useful at TDO 20-50 where Seq#5+ declines to EP=$6.
# Optimizer search space: [1, 2, 3, 4, 999]. Default 999 = disabled.
# Semantic note: in signal_smt this counts repeated divergence SIGNALS on the same level;
# in backtest_smt it counts ENTRY attempts. The two counts can differ when a signal fires
# but fails a filter (TDO_VALIDITY_CHECK, MIN_STOP_POINTS, etc.) before entry.
MAX_REENTRY_COUNT = 4

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
MIDNIGHT_OPEN_AS_TP: bool = False

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

# ── Solution F: Draw-on-Liquidity target selection ────────────────────────────
MIN_RR_FOR_TARGET: float = 1.5   # reward >= min_rr * |entry - stop|; optimizer: [1.0, 1.5, 2.0]
MIN_TARGET_PTS:    float = 15.0  # absolute min target distance in MNQ pts; optimizer: [10, 15, 20, 25]

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
MIN_DISPLACEMENT_PTS: float = 8.0

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
# After partial exit, slide the stop to partial_exit_price ± this buffer so the remaining
# contracts cannot lose significantly if price reverses before reaching TP.
# Set to 0.0 to lock the stop exactly at the partial price (no buffer).
# Optimizer search space: [0.5, 1.0, 2.0, 3.0]
PARTIAL_STOP_BUFFER_PTS: float = 0.5

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
SYMMETRIC_SMT_ENABLED: bool = True

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
DIV_SCORE_DECAY_INTERVAL:  int   = 10     # bars between each decay step (legacy)
# Wall-clock decay period — resolution-invariant replacement for DIV_SCORE_DECAY_INTERVAL.
# process_scan_bar converts this to bars via context.bar_seconds so decay rate is the same
# on 1m bars (backtest) and 1s bars (live). Default 600s = 10 minutes = 10 bars at 1m.
DIV_SCORE_DECAY_SECONDS: float = 600.0

# Adverse-move decay — additional decay based on how far price moved against hypothesis
ADVERSE_MOVE_FULL_DECAY_PTS:  float = 150.0  # pts of adverse move that drives score to floor; 999 = disabled
ADVERSE_MOVE_MIN_DECAY:       float = 0.10   # floor for the adverse-move decay multiplier

# ── Solution D: Hard invalidation threshold ───────────────────────────────────
HYPOTHESIS_INVALIDATION_PTS:  float = 999.0  # abandon hypothesis after this many pts adverse; 999 = disabled
HYPOTHESIS_FILTER:             bool  = False  # when True, only fire signals matching the HTF hypothesis direction

# ── Limit entry at anchor_close ───────────────────────────────────────────────
# Entry fills at anchor_close ± buffer instead of the confirmation bar's close.
# None = disabled (entry = bar["Close"], existing behaviour).
# 0.0  = exactly at anchor_close.
# >0   = anchor_close offset by buffer (SHORT: −buffer; LONG: +buffer).
# Optimizer search space: [1.0, 2.0, 3.0]
LIMIT_ENTRY_BUFFER_PTS: "float | None" = 1.0

# Forward-looking limit expiry window in wall-clock seconds.
# None = same-bar fill (LIMIT_ENTRY_BUFFER_PTS still controls entry_price;
#        no new state machine state).
# >0   = limit must fill on a subsequent bar within this window;
#        expired signals produce a limit_expired TSV record.
# Bar count derived automatically: max(1, round(LIMIT_EXPIRY_SECONDS / bar_seconds)).
# Typical values: 60 (1 min), 120 (2 min), 300 (5 min).
# Optimizer search space: [None, 60.0, 120.0, 300.0]
LIMIT_EXPIRY_SECONDS: "float | None" = 120.0

# Hybrid mode: bypass forward-looking limit for high-conviction bars.
# When entry_bar_body_ratio >= threshold, use same-bar fill regardless of
# LIMIT_EXPIRY_SECONDS. Only evaluated when LIMIT_EXPIRY_SECONDS is not None.
# None = all bars use forward-looking limit (no bypass).
# 0.60 = bars where the confirmation bar used >= 60% of its range as body use same-bar fill.
# Optimizer search space: [None, 0.40, 0.50, 0.60, 0.70]
LIMIT_RATIO_THRESHOLD: "float | None" = 0.70

# ── Lifecycle event types returned by process_scan_bar ────────────────────────
EVT_SIGNAL          = "signal"
EVT_LIMIT_PLACED    = "limit_placed"
EVT_LIMIT_MOVED     = "limit_moved"
EVT_LIMIT_CANCELLED = "limit_cancelled"
EVT_LIMIT_EXPIRED   = "limit_expired"
EVT_LIMIT_FILLED    = "limit_filled"

# Confirmation window size in bars.
# 1 = check every bar (default, current behaviour).
# N > 1 = build a synthetic N-bar candle (open of first bar, max high, min low, close of last)
# using a fixed window anchored to the divergence bar. Only check confirmation at window closes.
# Window 1: bars [div+1 .. div+N], window 2: [div+N+1 .. div+2N], etc.
# Named WINDOW_BARS (not MINUTES) because the value is a bar count — at 1s bars N=3 is 3 seconds.
CONFIRMATION_WINDOW_BARS: int = 1

# ── Human execution mode ─────────────────────────────────────────────────────
# When True, emits typed human-trader-friendly signals (ENTRY_MARKET / ENTRY_LIMIT /
# MOVE_STOP / CLOSE_MARKET), applies a confidence filter, and surfaces stop moves as
# MOVE_STOP events instead of silent mutations. Defaults False — zero behaviour change.
HUMAN_EXECUTION_MODE: bool = False
# Additive pts on top of existing slippage for market fills in human mode (default 0.0).
# Models human reaction delay; timeframe-agnostic (applies identically on 1m/5m/1s bars).
HUMAN_ENTRY_SLIPPAGE_PTS: float = 0.0
# Signals with confidence below this threshold are suppressed (logged) in human mode.
MIN_CONFIDENCE_THRESHOLD: float = 0.50
# DEPRECATED: no longer used by the classifier (signal_smt.py now derives signal_type
# from limit_fill_bars). Retained for optimizer compatibility only.
ENTRY_LIMIT_CLASSIFICATION_PTS: float = 5.0
# Minimum bar gap between consecutive MOVE_STOP signals; 0 = no rate limiting.
MOVE_STOP_MIN_GAP_BARS: int = 0
# Minimum bar gap between consecutive MOVE_LIMIT signals; 0 = no rate limiting.
# Distinct from MOVE_STOP_MIN_GAP_BARS — trader's tolerance for limit revisions
# is usually lower than for stop revisions.
MOVE_LIMIT_MIN_GAP_BARS: int = 0
# Opt-in deception-exit flag — active regardless of HUMAN_EXECUTION_MODE.
# When True, an opposing-direction displacement candle after entry triggers a market exit.
DECEPTION_OPPOSING_DISP_EXIT: bool = False

# ── Equal Highs / Equal Lows (Gap 1) ──────────────────────────────────────
# Master enable flag — when False, detect_eqh_eql returns ([], []) and no candidates
# flow into draws dict. Keep True by default after initial validation.
# Optimizer search space: [True, False]
EQH_ENABLED: bool = True
# Number of bars on each side for a fractal swing-point qualification.
# A bar at index i is a swing high iff its High beats all N bars on each side.
# Optimizer search space: [2, 3, 4]
EQH_SWING_BARS: int = 3
# Clustering tolerance in MNQ points. Two swing points within this distance
# are grouped into one EQH/EQL level at their mean price.
# Optimizer search space: [2.0, 3.0, 5.0, 8.0]
EQH_TOLERANCE_PTS: float = 3.0
# Minimum swing-point count to qualify as a valid EQH/EQL cluster.
# Single swing points (count=1) are plain PDH/PDL-style levels, not EQH/EQL.
# Optimizer search space: [2, 3]
EQH_MIN_TOUCHES: int = 2
# Lookback window in bars for the detection scan. ~100 bars = ~1.5 hrs of 1m data
# (overnight + prior-session window). Larger values find more candidates but
# include older, potentially-less-relevant levels.
# Optimizer search space: [50, 100, 200, 400]
EQH_LOOKBACK_BARS: int = 100


# ── Module-level bar data ─────────────────────────────────────────────────────
_mnq_bars: "pd.DataFrame | None" = None
_mes_bars: "pd.DataFrame | None" = None

# ── Cached time objects derived from SESSION_START (used in hot-path functions) ──
# These are module-level so they are computed once at import time, not per bar.
_SESSION_START_TIME = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
_SESSION_START_HOUR = _SESSION_START_TIME.hour
_SESSION_START_MIN  = _SESSION_START_TIME.minute


def set_bar_data(mnq_df: pd.DataFrame, mes_df: pd.DataFrame) -> None:
    """Populate module-level bar globals for strategy functions that need lookback.

    Reserved for multi-bar lookback logic (e.g. prior-session anchor, ATR filter).
    Called by run_backtest() and both 1m bar callbacks in signal_smt.
    """
    global _mnq_bars, _mes_bars
    _mnq_bars = mnq_df
    _mes_bars = mes_df


def init_bar_data(
    mnq_df: "pd.DataFrame | None" = None,
    mes_df: "pd.DataFrame | None" = None,
) -> None:
    """Initialise module-level bar globals, optionally from seed DataFrames.

    Call once before iterating bars. Pass seed DataFrames to pre-load historical
    data (e.g. realtime gap-fill on startup); call with no args to start empty
    (backtest, where bars are appended chronologically via append_bar_data).
    """
    global _mnq_bars, _mes_bars
    _empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    _mnq_bars = mnq_df.copy() if mnq_df is not None else _empty.copy()
    _mes_bars = mes_df.copy() if mes_df is not None else _empty.copy()


def append_bar_data(
    mnq_rows: "pd.DataFrame | None",
    mes_rows: "pd.DataFrame | None",
) -> None:
    """Append one or more bars to the module-level bar data globals.

    Accepts single-row or multi-row DataFrames. Pass None to skip updating
    that instrument (e.g. when MNQ and MES bars arrive in separate callbacks).
    """
    global _mnq_bars, _mes_bars
    if mnq_rows is not None and not mnq_rows.empty:
        _mnq_bars = mnq_rows.copy() if (_mnq_bars is None or _mnq_bars.empty) else pd.concat([_mnq_bars, mnq_rows])
    if mes_rows is not None and not mes_rows.empty:
        _mes_bars = mes_rows.copy() if (_mes_bars is None or _mes_bars.empty) else pd.concat([_mes_bars, mes_rows])


# ══ DATA STRUCTURES ══════════════════════════════════════════════════════════

class _BarRow:
    """Lightweight bar data holder — avoids pd.Series allocation in tight loops.

    Supports bar["Open"], bar["High"], bar["Low"], bar["Close"], bar["Volume"] and bar.name.
    """
    __slots__ = ("Open", "High", "Low", "Close", "Volume", "name")

    def __init__(self, Open: float, High: float, Low: float, Close: float,
                 Volume: float = 0.0, ts=None) -> None:
        self.Open   = Open
        self.High   = High
        self.Low    = Low
        self.Close  = Close
        self.Volume = Volume
        self.name   = ts

    def __getitem__(self, key: str) -> float:
        return getattr(self, key)


def _in_blackout(t: "datetime.time") -> bool:
    """Return True if wall-clock time t falls inside the signal blackout window.

    Reads SIGNAL_BLACKOUT_START/END dynamically so monkeypatching works in tests.
    Uses H*60+M integer comparison to avoid strftime overhead on every bar.
    """
    if not SIGNAL_BLACKOUT_START or not SIGNAL_BLACKOUT_END:
        return False
    _t = t.hour * 60 + t.minute
    _bh, _bm = int(SIGNAL_BLACKOUT_START[:2]), int(SIGNAL_BLACKOUT_START[3:])
    _eh, _em = int(SIGNAL_BLACKOUT_END[:2]),   int(SIGNAL_BLACKOUT_END[3:])
    return (_bh * 60 + _bm) <= _t < (_eh * 60 + _em)


def _in_silver_bullet(t: "datetime.time") -> bool:
    """Return True if wall-clock time t falls inside the silver-bullet window."""
    _t  = t.hour * 60 + t.minute
    _sh, _sm = int(SILVER_BULLET_START[:2]), int(SILVER_BULLET_START[3:])
    _eh, _em = int(SILVER_BULLET_END[:2]),   int(SILVER_BULLET_END[3:])
    return (_sh * 60 + _sm) <= _t < (_eh * 60 + _em)


# ══ STATEFUL SCANNER DATA STRUCTURES ═════════════════════════════════════════

class ScanState:
    """Mutable per-session scanning state — passed to process_scan_bar each bar.

    Encapsulates all local variables that the backtest IDLE / WAITING_FOR_ENTRY /
    REENTRY_ELIGIBLE / WAITING_FOR_LIMIT_FILL blocks currently keep as locals.
    Allows both backtest_smt.py and signal_smt.py to share a single strategy path.
    """
    __slots__ = (
        "scan_state",
        "pending_direction", "anchor_close",
        "divergence_bar_idx", "conf_window_start",
        "pending_smt_sweep", "pending_smt_miss",
        "pending_div_bar_high", "pending_div_bar_low",
        "pending_smt_defended", "pending_smt_type",
        "pending_fvg_zone", "pending_fvg_detected",
        "pending_displacement_bar_extreme",
        "pending_div_score", "pending_div_provisional",
        "pending_discovery_bar_idx", "pending_discovery_price",
        "pending_limit_signal",
        "limit_bars_elapsed", "limit_max_bars", "limit_missed_move",
        "reentry_count", "prior_trade_bars_held",
        "htf_state", "pending_htf_confirmed_tfs",
        "last_limit_move_bar_idx", "last_limit_signal_snapshot",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Reset all scanning state to session-start values."""
        self.scan_state                     = "IDLE"
        self.pending_direction              = None
        self.anchor_close                   = None
        self.divergence_bar_idx             = -1
        self.conf_window_start              = -1
        self.pending_smt_sweep              = 0.0
        self.pending_smt_miss               = 0.0
        self.pending_div_bar_high           = 0.0
        self.pending_div_bar_low            = 0.0
        self.pending_smt_defended           = None
        self.pending_smt_type               = "wick"
        self.pending_fvg_zone               = None
        self.pending_fvg_detected           = False
        self.pending_displacement_bar_extreme = None
        self.pending_div_score              = 0.0
        self.pending_div_provisional        = False
        self.pending_discovery_bar_idx      = -1
        self.pending_discovery_price        = 0.0
        self.pending_limit_signal           = None
        self.limit_bars_elapsed             = 0
        self.limit_max_bars                 = 0
        self.limit_missed_move              = 0.0
        self.reentry_count                  = 0
        self.prior_trade_bars_held          = 0
        # HTF state initialised to empty; caller populates when HTF_VISIBILITY_REQUIRED.
        self.htf_state: dict                = {}
        self.pending_htf_confirmed_tfs: "list | None" = None
        self.last_limit_move_bar_idx: int   = -999
        self.last_limit_signal_snapshot: "dict | None" = None


class SessionContext:
    """Immutable per-session data — computed once at session start, read-only during scan.

    Separates stable session metadata from the mutable ScanState so process_scan_bar
    can be called with both without conflating what changes and what doesn't.
    """
    __slots__ = (
        "day", "tdo", "midnight_open", "overnight",
        "pdh", "pdl", "hyp_ctx", "hyp_dir",
        "bar_seconds", "ref_lvls",
        "eqh_levels", "eql_levels",   # Gap 1
    )

    def __init__(
        self,
        day: "datetime.date",
        tdo: float,
        midnight_open: "float | None" = None,
        overnight: "dict | None" = None,
        pdh: "float | None" = None,
        pdl: "float | None" = None,
        hyp_ctx: "dict | None" = None,
        hyp_dir: "str | None" = None,
        bar_seconds: float = 60.0,
        ref_lvls: "dict | None" = None,
        eqh_levels: "list | None" = None,
        eql_levels: "list | None" = None,
    ) -> None:
        self.day          = day
        self.tdo          = tdo
        self.midnight_open = midnight_open
        self.overnight    = overnight or {}
        self.pdh          = pdh
        self.pdl          = pdl
        self.hyp_ctx      = hyp_ctx
        self.hyp_dir      = hyp_dir
        self.bar_seconds  = bar_seconds
        self.ref_lvls     = ref_lvls or {}
        self.eqh_levels   = eqh_levels or []
        self.eql_levels   = eql_levels or []


def build_synthetic_confirmation_bar(
    opens: "object",    # numpy array slice
    highs: "object",
    lows: "object",
    closes: "object",
    vols: "object",
    syn_start: int,
    bar_idx: int,
    ts,
) -> "_BarRow":
    """Build an N-bar synthetic candle from pre-extracted numpy arrays.

    Used when CONFIRMATION_WINDOW_BARS > 1: the Open is the first bar of the window,
    High/Low are the extremes, Close is the current bar, Volume is the sum.
    """
    return _BarRow(
        Open=float(opens[syn_start]),
        High=float(highs[syn_start:bar_idx + 1].max()),
        Low=float(lows[syn_start:bar_idx + 1].min()),
        Close=float(closes[bar_idx]),
        Volume=float(vols[syn_start:bar_idx + 1].sum()),
        ts=ts,
    )


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
      1. data/{ticker}_{interval}.parquet             — primary store
      2. data/{ticker}.parquet                        — legacy no-interval name
      3. FUTURES_CACHE_DIR/{interval}/{ticker}.parquet — IB ephemeral cache
    Returns {"MNQ": df, "MES": df} with tz-aware ET DatetimeIndex.
    Raises FileNotFoundError if parquets are missing (run prepare_futures_1m.py).
    """
    manifest = _load_futures_manifest()
    interval = manifest.get("fetch_interval", "1m")
    result: dict[str, pd.DataFrame] = {}
    for ticker in ["MNQ", "MES"]:
        primary_path    = Path("data") / f"{ticker}_{interval}.parquet"
        fallback_path   = Path("data") / f"{ticker}.parquet"
        ib_path         = Path(FUTURES_CACHE_DIR) / interval / f"{ticker}.parquet"
        if primary_path.exists():
            path = primary_path
        elif fallback_path.exists():
            path = fallback_path
        elif ib_path.exists():
            path = ib_path
        else:
            raise FileNotFoundError(
                f"Missing futures parquet for {ticker}. Run prepare_futures_1m.py."
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
    decay_interval_bars: "int | None" = None,
) -> float:
    """Apply time and adverse-move decay to a pending divergence score.

    decay_interval_bars: override for DIV_SCORE_DECAY_INTERVAL. Pass
    max(1, round(DIV_SCORE_DECAY_SECONDS / context.bar_seconds)) from
    process_scan_bar so decay rate is resolution-invariant.
    """
    _interval = decay_interval_bars if decay_interval_bars is not None else DIV_SCORE_DECAY_INTERVAL
    bars_held  = current_bar_idx - discovery_bar_idx
    time_decay = DIV_SCORE_DECAY_FACTOR ** (bars_held // max(_interval, 1))

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
    _cur_vals: "dict | None" = None,
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

    # Current bar values: use pre-extracted floats when available (_cur_vals),
    # otherwise fall back to DataFrame .iloc (slower pandas accessor path).
    if _cur_vals is not None:
        cur_mes_h = _cur_vals["mes_h"]
        cur_mes_l = _cur_vals["mes_l"]
        cur_mes_c = _cur_vals["mes_c"]
        cur_mnq_h = _cur_vals["mnq_h"]
        cur_mnq_l = _cur_vals["mnq_l"]
        cur_mnq_c = _cur_vals["mnq_c"]
    else:
        _cur_mes_row = mes_bars.iloc[bar_idx]
        _cur_mnq_row = mnq_bars.iloc[bar_idx]
        cur_mes_h = float(_cur_mes_row["High"])
        cur_mes_l = float(_cur_mes_row["Low"])
        cur_mes_c = float(_cur_mes_row["Close"])
        cur_mnq_h = float(_cur_mnq_row["High"])
        cur_mnq_l = float(_cur_mnq_row["Low"])
        cur_mnq_c = float(_cur_mnq_row["Close"])

    # Bearish SMT: MES sweeps session high (liquidity grab) but MNQ fails to confirm
    if cur_mes_h > mes_session_high and cur_mnq_h <= mnq_session_high:
        smt_sweep = cur_mes_h - mes_session_high
        mnq_miss   = mnq_session_high - cur_mnq_h
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("short", smt_sweep, mnq_miss, "wick", mnq_session_high)
    # Bullish SMT: MES sweeps session low but MNQ fails to confirm
    if cur_mes_l < mes_session_low and cur_mnq_l >= mnq_session_low:
        smt_sweep = mes_session_low - cur_mes_l
        mnq_miss   = cur_mnq_l - mnq_session_low
        if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
            return None
        if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
            return None
        return ("long", smt_sweep, mnq_miss, "wick", mnq_session_low)

    # Symmetric SMT: MNQ leads, MES fails (mirror of MES-leads logic above).
    if SYMMETRIC_SMT_ENABLED:
        if cur_mnq_h > mnq_session_high and cur_mes_h <= mes_session_high:
            smt_sweep = cur_mnq_h - mnq_session_high
            mnq_miss   = mnq_session_high - cur_mes_h
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("short", smt_sweep, mnq_miss, "wick_sym", mes_session_high)
        if cur_mnq_l < mnq_session_low and cur_mes_l >= mes_session_low:
            smt_sweep = mnq_session_low - cur_mnq_l
            mnq_miss   = cur_mes_l - mes_session_low
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
        if cur_mes_c > mes_close_session_high and cur_mnq_c <= mnq_close_session_high:
            smt_sweep = cur_mes_c - mes_close_session_high
            mnq_miss   = mnq_close_session_high - cur_mnq_c
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("short", smt_sweep, mnq_miss, "body", mnq_close_session_high)
        if cur_mes_c < mes_close_session_low and cur_mnq_c >= mnq_close_session_low:
            smt_sweep = mes_close_session_low - cur_mes_c
            mnq_miss   = cur_mnq_c - mnq_close_session_low
            if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
                return None
            if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
                return None
            return ("long", smt_sweep, mnq_miss, "body", mnq_close_session_low)
    return None



def compute_tdo(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """Return the "TDO" reference for a given date.

    Default (TDO_USE_MIDNIGHT=False): 09:30 AM ET RTH open (legacy behaviour).
    When TDO_USE_MIDNIGHT=True: routes to compute_midnight_open() — the ICT
    canonical 00:00 ET reference.

    Falls back to the first available bar on that date if the target bar is
    absent (e.g., for signals detected before 9:30 AM in the 9:00–9:30 window).
    Returns None if no bars exist for the date.
    """
    if TDO_USE_MIDNIGHT:
        return compute_midnight_open(mnq_bars, date)
    target_time = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")
    if target_time in mnq_bars.index:
        return float(mnq_bars.loc[target_time, "Open"])
    # Proxy: use the first available bar on that date (searchsorted avoids O(n) .date scan)
    day_start = pd.Timestamp(date, tz="America/New_York")
    day_end   = day_start + pd.Timedelta(days=1)
    pos_s = mnq_bars.index.searchsorted(day_start, side="left")
    pos_e = mnq_bars.index.searchsorted(day_end,   side="left")
    if pos_s >= pos_e:
        return None
    return float(mnq_bars.iloc[pos_s]["Open"])


def compute_midnight_open(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None:
    """Return the Open of the first 1m/5m bar at or after 00:00 ET on date.

    ICT canonical intraday reversion target. Falls back to the first bar on
    that date if no bar exists exactly at midnight (e.g. on 5m resampled data).
    Returns None if no bars exist for the date.
    """
    day_start = pd.Timestamp(date, tz="America/New_York")
    day_end   = day_start + pd.Timedelta(days=1)
    pos_s = mnq_bars.index.searchsorted(day_start, side="left")
    pos_e = mnq_bars.index.searchsorted(day_end,   side="left")
    if pos_s >= pos_e:
        return None
    midnight = pd.Timestamp(f"{date} 00:00:00", tz="America/New_York")
    pos_m = mnq_bars.index.searchsorted(midnight, side="left")
    if pos_m < pos_e:
        return float(mnq_bars.iloc[pos_m]["Open"])
    return float(mnq_bars.iloc[pos_s]["Open"])


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


def compute_pdh_pdl(
    hist_df: "pd.DataFrame",
    session_date: "datetime.date",
) -> "tuple[float | None, float | None]":
    """Return (previous_day_high, previous_day_low) from daily OHLCV.

    Uses the last row before session_date — safe with both 1m and daily data
    when hist_df has one row per prior calendar day (or resampled to daily).
    """
    if hist_df.empty or not hasattr(hist_df.index, "date"):
        return None, None
    prior = hist_df[hist_df.index.date < session_date]
    if prior.empty:
        return None, None
    prev = prior.iloc[-1]
    return float(prev["High"]), float(prev["Low"])


def _find_swing_points(
    highs,
    lows,
    start: int,
    end: int,
    swing_bars: int,
) -> "tuple[list[tuple[int, float]], list[tuple[int, float]]]":
    # Fractal swing detection: bar i qualifies iff its extreme strictly beats all
    # swing_bars neighbours on each side. Pure function — caller passes numpy arrays.
    swing_highs: "list[tuple[int, float]]" = []
    swing_lows:  "list[tuple[int, float]]" = []
    i_start = start + swing_bars
    # Forward window must fit: i + swing_bars <= end - 1 → i <= end - swing_bars - 1
    i_end   = end - swing_bars - 1
    for i in range(i_start, i_end + 1):
        h_i = highs[i]
        l_i = lows[i]
        is_high = True
        is_low  = True
        for j in range(1, swing_bars + 1):
            if highs[i - j] >= h_i or highs[i + j] >= h_i:
                is_high = False
            if lows[i - j] <= l_i or lows[i + j] <= l_i:
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            swing_highs.append((i, float(h_i)))
        if is_low:
            swing_lows.append((i, float(l_i)))
    return swing_highs, swing_lows


def _cluster_swing_points(
    points: "list[tuple[int, float]]",
    tolerance: float,
    min_touches: int,
) -> "list[dict]":
    # Greedy price-adjacent clustering: sort by price, accumulate into a running
    # cluster while next point is within `tolerance` of the cluster's mean price.
    if not points:
        return []
    sorted_pts = sorted(points, key=lambda p: p[1])
    clusters: "list[dict]" = []
    cur_prices: "list[float]" = []
    cur_idxs:   "list[int]"   = []
    cur_mean = 0.0
    for idx, price in sorted_pts:
        if not cur_prices:
            cur_prices.append(price)
            cur_idxs.append(idx)
            cur_mean = price
            continue
        if abs(price - cur_mean) <= tolerance:
            cur_prices.append(price)
            cur_idxs.append(idx)
            cur_mean = sum(cur_prices) / len(cur_prices)
        else:
            if len(cur_prices) >= min_touches:
                clusters.append({
                    "price":    cur_mean,
                    "touches":  len(cur_prices),
                    "last_bar": max(cur_idxs),
                })
            cur_prices = [price]
            cur_idxs   = [idx]
            cur_mean   = price
    if len(cur_prices) >= min_touches:
        clusters.append({
            "price":    cur_mean,
            "touches":  len(cur_prices),
            "last_bar": max(cur_idxs),
        })
    # Sort by touches desc, then last_bar desc (most recent tie-break)
    clusters.sort(key=lambda c: (-c["touches"], -c["last_bar"]))
    return clusters


def _filter_stale_levels(
    levels: "list[dict]",
    closes,
    bar_idx: int,
    direction: str,
) -> "list[dict]":
    # Stale = a close in (last_bar, bar_idx) has passed through the level
    # on the relevant side (above for EQH, below for EQL).
    if not levels:
        return []
    active: "list[dict]" = []
    if direction == "eqh":
        for lvl in levels:
            last_bar   = lvl["last_bar"]
            price      = lvl["price"]
            scan_start = last_bar + 1
            if scan_start >= bar_idx or not any(closes[k] > price for k in range(scan_start, bar_idx)):
                active.append(lvl)
    else:  # "eql"
        for lvl in levels:
            last_bar   = lvl["last_bar"]
            price      = lvl["price"]
            scan_start = last_bar + 1
            if scan_start >= bar_idx or not any(closes[k] < price for k in range(scan_start, bar_idx)):
                active.append(lvl)
    return active


def detect_eqh_eql(
    bars: pd.DataFrame,
    bar_idx: int,
    lookback: int = 100,
    swing_bars: int = 3,
    tolerance: float = 3.0,
    min_touches: int = 2,
) -> "tuple[list[dict], list[dict]]":
    """Return (eqh_levels, eql_levels) — clusters of equal swing highs/lows.

    Each level: {"price": float, "touches": int, "last_bar": int}.
    Returns ([], []) when EQH_ENABLED is False or insufficient data.
    Stale levels (any close in (last_bar, bar_idx) passed through the level) are removed.
    Results sorted by touches desc, then last_bar desc (most recent tie-break).
    """
    if not EQH_ENABLED:
        return [], []
    if len(bars) == 0:
        return [], []
    if bar_idx < 2 * swing_bars + min_touches:
        return [], []
    # Pre-extract numpy arrays — avoids pandas accessor overhead in the hot loop
    highs  = bars["High"].values
    lows   = bars["Low"].values
    closes = bars["Close"].values
    scan_start = max(0, bar_idx - lookback)
    scan_end   = bar_idx
    swing_highs, swing_lows = _find_swing_points(
        highs, lows, scan_start, scan_end, swing_bars
    )
    eqh_clusters = _cluster_swing_points(swing_highs, tolerance, min_touches)
    eql_clusters = _cluster_swing_points(swing_lows,  tolerance, min_touches)
    active_eqh = _filter_stale_levels(eqh_clusters, closes, bar_idx, "eqh")
    active_eql = _filter_stale_levels(eql_clusters, closes, bar_idx, "eql")
    return active_eqh, active_eql


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
    pdh: "float | None" = None,
    pdl: "float | None" = None,
    hyp_ctx: "dict | None" = None,
    hyp_dir: "str | None" = None,
) -> dict | None:
    """Thin wrapper over process_scan_bar — correct by construction; cannot diverge from backtest."""
    if mnq_bars.empty or mes_bars.empty:
        return None
    if tdo is None or tdo == 0.0:
        return None

    n_bars = min(len(mnq_bars), len(mes_bars))
    min_signal_ts = mnq_bars.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)
    mes_reset  = mes_bars.reset_index(drop=True)
    mnq_reset  = mnq_bars.reset_index(drop=True)
    _mnq_idx   = mnq_bars.index
    _mes_h     = mes_reset["High"].values
    _mes_l     = mes_reset["Low"].values
    _mes_c     = mes_reset["Close"].values
    _mnq_o     = mnq_reset["Open"].values
    _mnq_h     = mnq_reset["High"].values
    _mnq_l     = mnq_reset["Low"].values
    _mnq_c     = mnq_reset["Close"].values
    _mnq_v     = mnq_reset["Volume"].values if "Volume" in mnq_reset.columns else None

    _ref_lvls: dict = {}
    if EXPANDED_REFERENCE_LEVELS:
        _ref_lvls = _compute_ref_levels(
            prev_day_mnq, prev_day_mes, prev_session_mnq, prev_session_mes, None, None
        )

    _session_ctx = SessionContext(
        day=mnq_bars.index[0].date(),
        tdo=tdo,
        midnight_open=midnight_open,
        overnight=overnight_range or {},
        pdh=pdh,
        pdl=pdl,
        hyp_ctx=hyp_ctx,
        hyp_dir=hyp_dir,
        bar_seconds=60.0,
        ref_lvls=_ref_lvls,
        eqh_levels=[],
        eql_levels=[],
    )
    _scan_state  = ScanState()
    _smt_cache: dict = {
        "mes_h": float("nan"), "mes_l": float("nan"),
        "mnq_h": float("nan"), "mnq_l": float("nan"),
        "mes_ch": float("nan"), "mes_cl": float("nan"),
        "mnq_ch": float("nan"), "mnq_cl": float("nan"),
    }
    _ses_mes_h = _ses_mes_l = float("nan")
    _ses_mnq_h = _ses_mnq_l = float("nan")
    _ses_mes_ch = _ses_mes_cl = float("nan")
    _ses_mnq_ch = _ses_mnq_cl = float("nan")
    _run_ses_high = -float("inf")
    _run_ses_low  =  float("inf")

    for bar_idx in range(n_bars):
        if bar_idx > 0:
            _p = bar_idx - 1
            _v = float(_mes_h[_p])
            _ses_mes_h  = _v if _math.isnan(_ses_mes_h)  else max(_ses_mes_h,  _v)
            _v = float(_mes_l[_p])
            _ses_mes_l  = _v if _math.isnan(_ses_mes_l)  else min(_ses_mes_l,  _v)
            _v = float(_mnq_h[_p])
            _ses_mnq_h  = _v if _math.isnan(_ses_mnq_h)  else max(_ses_mnq_h,  _v)
            _run_ses_high = max(_run_ses_high, _v)
            _v = float(_mnq_l[_p])
            _ses_mnq_l  = _v if _math.isnan(_ses_mnq_l)  else min(_ses_mnq_l,  _v)
            _run_ses_low  = min(_run_ses_low,  _v)
            _v = float(_mes_c[_p])
            _ses_mes_ch = _v if _math.isnan(_ses_mes_ch) else max(_ses_mes_ch, _v)
            _ses_mes_cl = _v if _math.isnan(_ses_mes_cl) else min(_ses_mes_cl, _v)
            _v = float(_mnq_c[_p])
            _ses_mnq_ch = _v if _math.isnan(_ses_mnq_ch) else max(_ses_mnq_ch, _v)
            _ses_mnq_cl = _v if _math.isnan(_ses_mnq_cl) else min(_ses_mnq_cl, _v)

        _smt_cache["mes_h"]  = _ses_mes_h;  _smt_cache["mes_l"]  = _ses_mes_l
        _smt_cache["mnq_h"]  = _ses_mnq_h;  _smt_cache["mnq_l"]  = _ses_mnq_l
        _smt_cache["mes_ch"] = _ses_mes_ch; _smt_cache["mes_cl"] = _ses_mes_cl
        _smt_cache["mnq_ch"] = _ses_mnq_ch; _smt_cache["mnq_cl"] = _ses_mnq_cl

        ts  = _mnq_idx[bar_idx]
        bar = _BarRow(float(_mnq_o[bar_idx]), float(_mnq_h[bar_idx]),
                      float(_mnq_l[bar_idx]), float(_mnq_c[bar_idx]),
                      float(_mnq_v[bar_idx]) if _mnq_v is not None else 0.0, ts)

        result = process_scan_bar(
            _scan_state, _session_ctx, bar_idx, bar,
            mnq_reset, mes_reset, _smt_cache,
            _run_ses_high, _run_ses_low, ts, min_signal_ts,
            _mnq_o, _mnq_h, _mnq_l, _mnq_c,
            _mnq_v if _mnq_v is not None else _mnq_c,  # vols fallback
            _mes_h, _mes_l, _mes_c,
        )
        if result is not None:
            if result["type"] == "lifecycle_batch":
                _sig_evts = [e for e in result["events"] if e["type"] == "signal"]
                if _sig_evts:
                    return _sig_evts[0]
            elif result["type"] == "signal":
                return result

    return None


def select_draw_on_liquidity(
    direction: str,
    entry_price: float,
    stop_price: float,
    draws: dict,
    min_rr: float = 1.5,
    min_pts: float = 15.0,
) -> "tuple[str | None, float | None, str | None, float | None, list]":
    """Return (primary_name, primary_price, secondary_name, secondary_price, valid_draws).

    Primary = nearest draw satisfying both RR and pts constraints.
    Secondary = next farther draw at least 1.5x primary distance away.
    valid_draws = full sorted list of (name, price, dist) tuples that passed the filter.
    Returns (None, None, None, None, []) if no valid draw exists.
    """
    stop_dist = abs(entry_price - stop_price)
    min_dist  = max(min_pts, min_rr * stop_dist)
    valid: "list[tuple[str, float, float]]" = []
    for name, price in draws.items():
        if price is None:
            continue
        dist = (price - entry_price) if direction == "long" else (entry_price - price)
        if dist >= min_dist:
            valid.append((name, price, dist))
    if not valid:
        return None, None, None, None, []
    valid.sort(key=lambda x: x[2])
    pri = valid[0]
    sec = next((v for v in valid[1:] if v[2] >= pri[2] * 1.5), None)
    return pri[0], pri[1], (sec[0] if sec else None), (sec[1] if sec else None), valid


def compute_confidence(
    signal: dict,
    prior_session_profitable: bool,
    session_start_ts: "pd.Timestamp",
) -> float:
    """Confidence score in [0, 1] for a built signal, used by human-execution mode.

    Weights:
      0.4 — time-of-day (ramps 0→1 between session start and +210 min)
      0.3 — prior-session profitability (binary)
      0.2 — displacement body size (normalised to 20 pts)
      0.1 — TDO distance sweet spot (peak at 30–100 pts, zero beyond 200)

    Weights are v0 hand-tuned; expected to be tuned by the optimizer.
    """
    entry_ts = signal["entry_time"]
    # Align tz between entry and session-start: either both aware or both naive.
    if session_start_ts.tz is not None and entry_ts.tz is None:
        entry_ts = entry_ts.tz_localize(session_start_ts.tz)
    elif session_start_ts.tz is None and entry_ts.tz is not None:
        entry_ts = entry_ts.tz_localize(None)
    mins_since_start = max(0.0, (entry_ts - session_start_ts).total_seconds() / 60.0)
    time_score = min(1.0, mins_since_start / 210.0)   # 210 min = session start to +3.5h

    trend_score = 1.0 if prior_session_profitable else 0.0

    body_pts = signal.get("displacement_body_pts") or 0.0
    body_score = min(1.0, body_pts / 20.0) if body_pts > 0 else 0.0

    tdo_dist = abs(signal["entry_price"] - signal["tdo"])
    if tdo_dist < 30.0:
        dist_score = tdo_dist / 30.0
    elif tdo_dist <= 100.0:
        dist_score = 1.0
    elif tdo_dist <= 200.0:
        dist_score = 1.0 - (tdo_dist - 100.0) / 100.0
    else:
        dist_score = 0.0

    return round(
        0.4 * time_score + 0.3 * trend_score + 0.2 * body_score + 0.1 * dist_score,
        4,
    )


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
    # Limit entry fields:
    anchor_close: "float | None" = None,
) -> dict | None:
    """Build a signal dict from a confirmed entry bar, applying all validity guards.

    Returns None if the signal fails TDO_VALIDITY_CHECK, MIN_STOP_POINTS,
    MIN_TDO_DISTANCE_PTS, or MAX_TDO_DISTANCE_PTS guards.
    """
    if anchor_close is not None and LIMIT_ENTRY_BUFFER_PTS is not None:
        entry_price = (
            anchor_close - LIMIT_ENTRY_BUFFER_PTS if direction == "short"
            else anchor_close + LIMIT_ENTRY_BUFFER_PTS
        )
    else:
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

    signal = {
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
        # Limit entry diagnostics
        "anchor_close_price": anchor_close if LIMIT_ENTRY_BUFFER_PTS is not None else None,
        "limit_fill_bars":    None,   # populated by state machine on fill
        "missed_move_pts":    None,   # populated by state machine on expiry
        "initial_stop_pts":   round(abs(entry_price - round(stop_price, 4)), 4),
        # Human-mode fields (Wave 3) — signal_type is re-classified in signal_smt.py
        # against live price at emit time; default here is ENTRY_MARKET.
        "signal_type":        "ENTRY_MARKET",
        "confidence":         None,
    }

    # Confidence: session start is the bar's day at SESSION_START in the bar's tz.
    # Uses module-level cached hour/minute to avoid recreating pd.Timestamp per call.
    if hasattr(ts, "normalize"):
        session_start_ts = ts.normalize().replace(
            hour=_SESSION_START_HOUR, minute=_SESSION_START_MIN, second=0
        )
    else:
        session_start_ts = ts  # fallback; tests pass Timestamp objects
    # prior_session_profitable wired as False here; backtest/signal wrappers can override
    # via a later field-set once session-level PnL bookkeeping is threaded in.
    signal["confidence"] = compute_confidence(signal, False, session_start_ts)
    return signal


def _annotate_hypothesis(signal: dict, hyp_ctx: "dict | None", hyp_dir: "str | None") -> None:
    """Stamp hypothesis-match and per-rule breakdown fields onto a signal dict in-place."""
    signal["matches_hypothesis"] = (
        (signal.get("direction") == hyp_dir) if hyp_dir is not None else None
    )
    if hyp_ctx is not None:
        signal["hypothesis_direction"] = hyp_ctx.get("direction")
        signal["pd_range_case"]        = hyp_ctx.get("pd_range_case")
        signal["pd_range_bias"]        = hyp_ctx.get("pd_range_bias")
        signal["week_zone"]            = hyp_ctx.get("week_zone")
        signal["day_zone"]             = hyp_ctx.get("day_zone")
        signal["trend_direction"]      = hyp_ctx.get("trend_direction")
        signal["hypothesis_score"]     = hyp_ctx.get("hypothesis_score", 0)
    else:
        signal["hypothesis_direction"] = None
        signal["pd_range_case"]        = None
        signal["pd_range_bias"]        = None
        signal["week_zone"]            = None
        signal["day_zone"]             = None
        signal["trend_direction"]      = None
        signal["hypothesis_score"]     = 0


def _build_draws_and_select(
    direction: str, ep: float, sp: float, fvg_zone: "dict | None",
    day_tdo: "float | None", midnight_open: "float | None",
    run_ses_high: float, run_ses_low: float,
    overnight: dict, pdh: "float | None", pdl: "float | None",
    eqh_levels: "list | None" = None,
    eql_levels: "list | None" = None,
) -> "tuple[str | None, float | None, str | None, float | None, list]":
    """Assemble draw-on-liquidity targets and delegate to select_draw_on_liquidity. Returns 5-tuple including full valid_draws list."""
    if direction == "long":
        draws = {
            "fvg_top":        fvg_zone["fvg_high"] if fvg_zone else None,
            "tdo":            day_tdo if day_tdo and day_tdo > ep else None,
            "midnight_open":  midnight_open if midnight_open and midnight_open > ep else None,
            "session_high":   run_ses_high if run_ses_high > ep + 1 else None,
            "overnight_high": overnight.get("overnight_high") if overnight.get("overnight_high") and overnight.get("overnight_high") > ep else None,
            "pdh":            pdh if pdh and pdh > ep else None,
        }
        if eqh_levels:
            # Nearest active EQH ABOVE entry — EQH is only a meaningful long TP above ep.
            nearest = next(
                (lvl for lvl in sorted(eqh_levels, key=lambda l: l["price"]) if lvl["price"] > ep),
                None,
            )
            draws["eqh"] = nearest["price"] if nearest else None
    else:
        draws = {
            "fvg_bottom":    fvg_zone["fvg_low"] if fvg_zone else None,
            "tdo":           day_tdo if day_tdo and day_tdo < ep else None,
            "midnight_open": midnight_open if midnight_open and midnight_open < ep else None,
            "session_low":   run_ses_low if run_ses_low < ep - 1 else None,
            "overnight_low": overnight.get("overnight_low") if overnight.get("overnight_low") and overnight.get("overnight_low") < ep else None,
            "pdl":           pdl if pdl and pdl < ep else None,
        }
        if eql_levels:
            # Nearest active EQL BELOW entry
            nearest = next(
                (lvl for lvl in sorted(eql_levels, key=lambda l: -l["price"]) if lvl["price"] < ep),
                None,
            )
            draws["eql"] = nearest["price"] if nearest else None
    return select_draw_on_liquidity(direction, ep, sp, draws, MIN_RR_FOR_TARGET, MIN_TARGET_PTS)


def _build_preliminary_limit_signal(
    state: "ScanState",
    context: "SessionContext",
    ts: "pd.Timestamp",
    run_ses_high: float,
    run_ses_low: float,
) -> "dict | None":
    """Build a preliminary limit-order signal at divergence time for LIMIT_PLACED emission.

    Uses the divergence bar's wick extremes for stop rather than a confirmation bar.
    Does not run TDO_VALIDITY_CHECK or displacement guards (confirmation-side only).
    Returns None if TP selection fails — divergence state is preserved by caller.
    """
    if LIMIT_ENTRY_BUFFER_PTS is None or context.tdo is None:
        return None
    direction = state.pending_direction
    ac = state.anchor_close
    entry_price = (
        ac - LIMIT_ENTRY_BUFFER_PTS if direction == "short"
        else ac + LIMIT_ENTRY_BUFFER_PTS
    )
    if STRUCTURAL_STOP_MODE:
        if direction == "short":
            stop_price = state.pending_div_bar_high + STRUCTURAL_STOP_BUFFER_PTS
        else:
            stop_price = state.pending_div_bar_low - STRUCTURAL_STOP_BUFFER_PTS
    else:
        dist = abs(entry_price - context.tdo)
        stop_ratio = SHORT_STOP_RATIO if direction == "short" else LONG_STOP_RATIO
        stop_price = (
            entry_price + stop_ratio * dist if direction == "short"
            else entry_price - stop_ratio * dist
        )
    # Reject inverted stops
    if direction == "long" and stop_price >= entry_price:
        return None
    if direction == "short" and stop_price <= entry_price:
        return None
    tp_name, day_tp, sec_tp_name, sec_tp, _ = _build_draws_and_select(
        direction, entry_price, stop_price, state.pending_fvg_zone,
        context.tdo, context.midnight_open, run_ses_high, run_ses_low,
        context.overnight, context.pdh, context.pdl,
        eqh_levels=context.eqh_levels, eql_levels=context.eql_levels,
    )
    if day_tp is None:
        return None
    return {
        "direction":              direction,
        "entry_price":            round(entry_price, 4),
        "stop_price":             round(stop_price, 4),
        "take_profit":            day_tp,
        "tp_name":                tp_name,
        "secondary_target":       sec_tp,
        "secondary_target_name":  sec_tp_name,
        "tdo":                    context.tdo,
        "divergence_bar":         state.divergence_bar_idx,
        "divergence_bar_time":    str(ts),
        "signal_type":            "ENTRY_LIMIT",
        "anchor_close_price":     ac,
    }


def process_scan_bar(
    state: "ScanState",
    context: "SessionContext",
    bar_idx: int,
    bar: "_BarRow",
    mnq_reset: pd.DataFrame,
    mes_reset: pd.DataFrame,
    smt_cache: dict,
    run_ses_high: float,
    run_ses_low: float,
    ts: "pd.Timestamp",
    min_signal_ts: "pd.Timestamp",
    mnq_opens,
    mnq_highs,
    mnq_lows,
    mnq_closes,
    mnq_vols,
    mes_highs=None,
    mes_lows=None,
    mes_closes=None,
) -> "dict | None":
    """Stateful scanner: process one bar of the non-IN_TRADE state machine.

    Returns:
        None                     — nothing actionable this bar
        {"type": "signal", ...}  — entry signal; caller handles position sizing
        {"type": "expired", "signal": ..., "limit_missed_move": float}
                                 — limit order expired without fill

    Mutates state in place. Never builds trade records or sizes contracts.
    """
    # ── HTF state initialization (first call or empty state) ─────────────────
    if HTF_VISIBILITY_REQUIRED and not state.htf_state:
        for _T in HTF_PERIODS_MINUTES:
            state.htf_state[_T] = {
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

    # ── HTF period extreme update ─────────────────────────────────────────────
    if HTF_VISIBILITY_REQUIRED and state.htf_state and mes_highs is not None:
        _ts_ns = ts.value
        for _T, _hs in state.htf_state.items():
            _period_ns = _T * 60 * 10**9
            _pstart = (_ts_ns // _period_ns) * _period_ns
            if _hs["pstart"] != _pstart:
                if _hs["pstart"] is not None and not _math.isnan(_hs["c_mes_h"]):
                    _hs["p_mes_h"]  = _hs["c_mes_h"];  _hs["p_mes_l"]  = _hs["c_mes_l"]
                    _hs["p_mnq_h"]  = _hs["c_mnq_h"];  _hs["p_mnq_l"]  = _hs["c_mnq_l"]
                    _hs["p_mes_ch"] = _hs["c_mes_ch"]; _hs["p_mes_cl"] = _hs["c_mes_cl"]
                    _hs["p_mnq_ch"] = _hs["c_mnq_ch"]; _hs["p_mnq_cl"] = _hs["c_mnq_cl"]
                _hs["pstart"]   = _pstart
                _hs["c_mes_h"]  = float(mes_highs[bar_idx])
                _hs["c_mes_l"]  = float(mes_lows[bar_idx])
                _hs["c_mnq_h"]  = float(mnq_highs[bar_idx])
                _hs["c_mnq_l"]  = float(mnq_lows[bar_idx])
                _hs["c_mes_ch"] = float(mes_closes[bar_idx])
                _hs["c_mes_cl"] = float(mes_closes[bar_idx])
                _hs["c_mnq_ch"] = float(mnq_closes[bar_idx])
                _hs["c_mnq_cl"] = float(mnq_closes[bar_idx])
            else:
                _hs["c_mes_h"]  = max(_hs["c_mes_h"],  float(mes_highs[bar_idx]))
                _hs["c_mes_l"]  = min(_hs["c_mes_l"],  float(mes_lows[bar_idx]))
                _hs["c_mnq_h"]  = max(_hs["c_mnq_h"],  float(mnq_highs[bar_idx]))
                _hs["c_mnq_l"]  = min(_hs["c_mnq_l"],  float(mnq_lows[bar_idx]))
                _hs["c_mes_ch"] = max(_hs["c_mes_ch"], float(mes_closes[bar_idx]))
                _hs["c_mes_cl"] = min(_hs["c_mes_cl"], float(mes_closes[bar_idx]))
                _hs["c_mnq_ch"] = max(_hs["c_mnq_ch"], float(mnq_closes[bar_idx]))
                _hs["c_mnq_cl"] = min(_hs["c_mnq_cl"], float(mnq_closes[bar_idx]))

    # ── WAITING_FOR_LIMIT_FILL ────────────────────────────────────────────────
    if state.scan_state == "WAITING_FOR_LIMIT_FILL":
        state.limit_bars_elapsed += 1
        pls = state.pending_limit_signal

        if pls["direction"] == "short":
            state.limit_missed_move = max(
                state.limit_missed_move,
                pls["entry_price"] - float(bar["Low"])
            )
        else:
            state.limit_missed_move = max(
                state.limit_missed_move,
                float(bar["High"]) - pls["entry_price"]
            )

        filled = (
            (pls["direction"] == "short" and float(bar["Low"]) <= pls["entry_price"])
            or (pls["direction"] == "long" and float(bar["High"]) >= pls["entry_price"])
        )

        if filled:
            pls["limit_fill_bars"] = state.limit_bars_elapsed
            signal_out = dict(pls)
            state.reentry_count += 1
            signal_out["reentry_sequence"]      = state.reentry_count
            signal_out["prior_trade_bars_held"] = state.prior_trade_bars_held
            state.last_limit_signal_snapshot = None
            state.pending_limit_signal = None
            state.scan_state = "IDLE"
            _filled_evt = {
                "type":                  EVT_LIMIT_FILLED,
                "direction":             pls["direction"],
                "filled_price":          pls["entry_price"],
                "original_limit_price":  pls.get("anchor_close_price", pls["entry_price"]),
                "time_in_queue_secs":    state.limit_bars_elapsed * context.bar_seconds,
                "divergence_bar_time":   pls.get("divergence_bar_time"),
            }
            return {"type": "lifecycle_batch", "events": [_filled_evt, {"type": "signal", **signal_out}]}

        if state.limit_bars_elapsed >= state.limit_max_bars:
            expired_out = {
                "type":              EVT_LIMIT_EXPIRED,
                "signal":            dict(pls),
                "limit_missed_move": state.limit_missed_move,
            }
            state.last_limit_signal_snapshot = None
            state.pending_limit_signal = None
            state.anchor_close = None
            state.scan_state = "IDLE"
            return expired_out

        return None

    # ── WAITING_FOR_ENTRY / REENTRY_ELIGIBLE ─────────────────────────────────
    if state.scan_state in ("WAITING_FOR_ENTRY", "REENTRY_ELIGIBLE"):
        if state.scan_state == "REENTRY_ELIGIBLE":
            if MAX_REENTRY_COUNT < 999 and state.reentry_count >= MAX_REENTRY_COUNT:
                _snap = state.last_limit_signal_snapshot
                state.scan_state = "IDLE"
                state.pending_direction = None
                state.anchor_close = None
                state.conf_window_start = -1
                state.last_limit_signal_snapshot = None
                if _snap is not None:
                    return {"type": EVT_LIMIT_CANCELLED, "signal": _snap, "reason": "max_reentry"}
                return None

        if _in_blackout(ts.time()):
            return None

        # WAITING_FOR_ENTRY: replacement detection + adversarial check
        if state.scan_state == "WAITING_FOR_ENTRY":
            # Replacement check — skip during the active N-bar confirmation window so
            # the window counter cannot be reset before firing. With CONFIRMATION_WINDOW_BARS=1
            # only the first candidate bar is protected; later bars allow replacement.
            _in_active_window = (
                state.conf_window_start >= 0
                and bar_idx >= state.conf_window_start
                and bar_idx < state.conf_window_start + CONFIRMATION_WINDOW_BARS
            )
            if not _in_active_window:
                _wfe_cur_vals = {
                    "mes_h": float(mes_highs[bar_idx]) if mes_highs is not None else float(mes_reset.iloc[bar_idx]["High"]),
                    "mes_l": float(mes_lows[bar_idx])  if mes_lows  is not None else float(mes_reset.iloc[bar_idx]["Low"]),
                    "mes_c": float(mes_closes[bar_idx]) if mes_closes is not None else float(mes_reset.iloc[bar_idx]["Close"]),
                    "mnq_h": float(mnq_highs[bar_idx]),
                    "mnq_l": float(mnq_lows[bar_idx]),
                    "mnq_c": float(mnq_closes[bar_idx]),
                }
                _new_div = detect_smt_divergence(
                    mes_reset, mnq_reset, bar_idx, 0, _cached=smt_cache, _cur_vals=_wfe_cur_vals
                )
            else:
                _new_div = None
            if _new_div is not None:
                _nd_dir, _nd_sweep, _nd_miss, _nd_type, _nd_defended = _new_div
                _nd_body = abs(float(bar["Close"]) - float(bar["Open"]))
                _nd_score = divergence_score(
                    _nd_sweep, _nd_miss, _nd_body, _nd_type, context.hyp_dir, _nd_dir
                )
                if _nd_score >= MIN_DIV_SCORE:
                    _decay_ivl = max(1, round(DIV_SCORE_DECAY_SECONDS / context.bar_seconds))
                    _eff = _effective_div_score(
                        state.pending_div_score,
                        state.pending_discovery_bar_idx, bar_idx,
                        state.pending_discovery_price, state.pending_direction,
                        float(bar["High"]), float(bar["Low"]),
                        _decay_ivl,
                    )
                    _replace = False
                    if state.pending_smt_type in ("wick", "body") and _nd_type == "displacement":
                        _replace = False
                    elif state.pending_div_provisional and _nd_type in ("wick", "body"):
                        _replace = True
                    elif _nd_dir == state.pending_direction and _nd_score > _eff:
                        _replace = True
                    elif _nd_dir != state.pending_direction and _nd_score > _eff * REPLACE_THRESHOLD:
                        _replace = True

                    if _replace:
                        _new_ac = find_anchor_close(mnq_reset, bar_idx, _nd_dir)
                        if _new_ac is not None:
                            fvg_lookback = max(3, round(1200 / context.bar_seconds))
                            _new_fvg = detect_fvg(mnq_reset, bar_idx, _nd_dir, lookback=fvg_lookback)
                            _old_direction = state.pending_direction
                            state.pending_direction           = _nd_dir
                            state.anchor_close                = _new_ac
                            state.pending_smt_sweep           = _nd_sweep
                            state.pending_smt_miss            = _nd_miss
                            state.pending_div_bar_high        = float(bar["High"])
                            state.pending_div_bar_low         = float(bar["Low"])
                            state.pending_smt_defended        = _nd_defended
                            state.pending_smt_type            = _nd_type
                            state.pending_div_score           = _nd_score
                            state.pending_div_provisional     = (_nd_type == "displacement")
                            state.pending_discovery_bar_idx   = bar_idx
                            state.pending_discovery_price     = float(bar["Close"])
                            state.divergence_bar_idx          = bar_idx
                            state.pending_displacement_bar_extreme = (
                                float(bar["Low"]) if _nd_dir == "long" else float(bar["High"])
                            ) if _nd_type == "displacement" else None
                            state.pending_fvg_zone            = _new_fvg
                            state.pending_fvg_detected        = _new_fvg is not None
                            state.conf_window_start           = bar_idx + 1
                            _old_snap = state.last_limit_signal_snapshot
                            _new_prelim = _build_preliminary_limit_signal(
                                state, context, ts, run_ses_high, run_ses_low
                            )
                            if _new_prelim is not None:
                                _rate_ok = (
                                    MOVE_LIMIT_MIN_GAP_BARS == 0
                                    or bar_idx - state.last_limit_move_bar_idx >= MOVE_LIMIT_MIN_GAP_BARS
                                )
                                if _rate_ok:
                                    state.last_limit_move_bar_idx = bar_idx
                                    state.last_limit_signal_snapshot = dict(_new_prelim)
                                    if _nd_dir == _old_direction and _old_snap is not None:
                                        _prices_changed = (
                                            _new_prelim["entry_price"] != _old_snap.get("entry_price")
                                            or _new_prelim["stop_price"] != _old_snap.get("stop_price")
                                            or _new_prelim["take_profit"] != _old_snap.get("take_profit")
                                        )
                                        if _prices_changed:
                                            return {
                                                "type": EVT_LIMIT_MOVED,
                                                "old_signal": _old_snap,
                                                "new_signal": dict(_new_prelim),
                                            }
                                    elif _nd_dir != _old_direction and _old_snap is not None:
                                        return {
                                            "type": "lifecycle_batch",
                                            "events": [
                                                {"type": EVT_LIMIT_CANCELLED, "signal": _old_snap, "reason": "direction_replaced"},
                                                {"type": EVT_LIMIT_PLACED, "signal": dict(_new_prelim)},
                                            ],
                                        }
                                    elif _old_snap is None:
                                        return {"type": EVT_LIMIT_PLACED, "signal": dict(_new_prelim)}
                                else:
                                    state.last_limit_signal_snapshot = dict(_new_prelim)

            # Solution D: abandon hypothesis if adverse move exceeds threshold
            if HYPOTHESIS_INVALIDATION_PTS < 999:
                if state.pending_direction == "short":
                    _adverse = float(bar["High"]) - state.pending_discovery_price
                else:
                    _adverse = state.pending_discovery_price - float(bar["Low"])
                if _adverse > HYPOTHESIS_INVALIDATION_PTS:
                    _snap = state.last_limit_signal_snapshot
                    state.scan_state = "IDLE"
                    state.pending_direction = None
                    state.anchor_close = None
                    state.last_limit_signal_snapshot = None
                    if _snap is not None:
                        return {"type": EVT_LIMIT_CANCELLED, "signal": _snap, "reason": "hypothesis_invalidated"}
                    return None

        # Build confirmation bar: raw bar or synthetic N-bar candle
        _conf_bar = bar
        if CONFIRMATION_WINDOW_BARS > 1:
            if (bar_idx < state.conf_window_start
                    or (bar_idx - state.conf_window_start + 1) % CONFIRMATION_WINDOW_BARS != 0):
                _conf_bar = None
            else:
                _syn_start = bar_idx - CONFIRMATION_WINDOW_BARS + 1
                _conf_bar = build_synthetic_confirmation_bar(
                    mnq_opens, mnq_highs, mnq_lows, mnq_closes, mnq_vols,
                    _syn_start, bar_idx, ts
                )

        if _conf_bar is None or state.anchor_close is None:
            return None

        if not is_confirmation_bar(_conf_bar, state.anchor_close, state.pending_direction):
            _is_adverse = (
                (state.pending_direction == "short"
                 and float(_conf_bar["Close"]) > float(_conf_bar["Open"]))
                or (state.pending_direction == "long"
                    and float(_conf_bar["Close"]) < float(_conf_bar["Open"]))
            )
            if _is_adverse:
                state.anchor_close = float(_conf_bar["Close"])
            return None

        # ALWAYS_REQUIRE_CONFIRMATION: bar must break divergence bar's body boundary
        if ALWAYS_REQUIRE_CONFIRMATION and state.divergence_bar_idx >= 0:
            _div = mnq_reset.iloc[state.divergence_bar_idx]
            _dbh = max(float(_div["Open"]), float(_div["Close"]))
            _dbl = min(float(_div["Open"]), float(_div["Close"]))
            if state.pending_direction == "short" and float(_conf_bar["Close"]) >= _dbl:
                return None
            if state.pending_direction == "long" and float(_conf_bar["Close"]) <= _dbh:
                return None

        signal = _build_signal_from_bar(
            _conf_bar, ts, state.pending_direction, context.tdo,
            smt_sweep_pts=state.pending_smt_sweep,
            smt_miss_pts=state.pending_smt_miss,
            divergence_bar_idx=state.divergence_bar_idx,
            divergence_bar_high=state.pending_div_bar_high,
            divergence_bar_low=state.pending_div_bar_low,
            midnight_open=context.midnight_open,
            smt_defended_level=state.pending_smt_defended,
            smt_type=state.pending_smt_type,
            fvg_zone=state.pending_fvg_zone,
            anchor_close=state.anchor_close,
        )
        if signal is None:
            return None

        signal["entry_bar"] = bar_idx

        _ep = signal["entry_price"]
        _sp = signal["stop_price"]
        _tp_name, _day_tp, _sec_tp_name, _sec_tp, _valid_draws = _build_draws_and_select(
            state.pending_direction, _ep, _sp, state.pending_fvg_zone,
            context.tdo, context.midnight_open, run_ses_high, run_ses_low,
            context.overnight, context.pdh, context.pdl,
            eqh_levels=context.eqh_levels, eql_levels=context.eql_levels,
        )
        if _day_tp is None:
            return None

        signal["take_profit"] = _day_tp
        signal["tp_name"]     = _tp_name
        if signal.get("partial_exit_level") is not None:
            signal["partial_exit_level"] = round(
                _ep + (_day_tp - _ep) * PARTIAL_EXIT_LEVEL_RATIO, 4
            )
        _annotate_hypothesis(signal, context.hyp_ctx, context.hyp_dir)
        signal["fvg_detected"] = state.pending_fvg_detected
        signal["htf_confirmed_timeframes"] = state.pending_htf_confirmed_tfs

        if HYPOTHESIS_FILTER and signal.get("matches_hypothesis") is not True:
            _snap = state.last_limit_signal_snapshot
            state.scan_state = "IDLE"
            state.pending_direction = None
            state.anchor_close = None
            state.last_limit_signal_snapshot = None
            if _snap is not None:
                return {"type": EVT_LIMIT_CANCELLED, "signal": _snap, "reason": "hypothesis_filter_miss"}
            return None

        _body_ratio = signal.get("entry_bar_body_ratio", 0.0)
        _use_forward_limit = (
            LIMIT_ENTRY_BUFFER_PTS is not None
            and LIMIT_EXPIRY_SECONDS is not None
            and (LIMIT_RATIO_THRESHOLD is None or _body_ratio < LIMIT_RATIO_THRESHOLD)
        )

        if _use_forward_limit:
            signal["secondary_target"]      = _sec_tp
            signal["secondary_target_name"] = _sec_tp_name
            if DISPLACEMENT_STOP_MODE and signal.get("smt_type") == "displacement":
                _extreme = state.pending_displacement_bar_extreme
                if _extreme is not None:
                    if signal["direction"] == "long":
                        signal["stop_price"] = _extreme - STRUCTURAL_STOP_BUFFER_PTS
                    else:
                        signal["stop_price"] = _extreme + STRUCTURAL_STOP_BUFFER_PTS
            state.scan_state = "WAITING_FOR_LIMIT_FILL"
            state.pending_limit_signal = signal
            state.limit_bars_elapsed = 0
            state.limit_max_bars = max(1, round(LIMIT_EXPIRY_SECONDS / context.bar_seconds))
            state.limit_missed_move = 0.0
            return None

        signal["limit_fill_bars"] = 0 if LIMIT_ENTRY_BUFFER_PTS is not None else None
        if DISPLACEMENT_STOP_MODE and signal.get("smt_type") == "displacement":
            _extreme = state.pending_displacement_bar_extreme
            if _extreme is not None:
                if signal["direction"] == "long":
                    signal["stop_price"] = _extreme - STRUCTURAL_STOP_BUFFER_PTS
                else:
                    signal["stop_price"] = _extreme + STRUCTURAL_STOP_BUFFER_PTS
        signal["secondary_target"]      = _sec_tp
        signal["secondary_target_name"] = _sec_tp_name
        state.reentry_count += 1
        signal["reentry_sequence"]      = state.reentry_count
        signal["prior_trade_bars_held"] = state.prior_trade_bars_held
        state.last_limit_signal_snapshot = None
        state.scan_state = "IDLE"
        if LIMIT_ENTRY_BUFFER_PTS is not None:
            _filled_evt = {
                "type":                 EVT_LIMIT_FILLED,
                "direction":            signal["direction"],
                "filled_price":         signal["entry_price"],
                "original_limit_price": signal.get("anchor_close_price", signal["entry_price"]),
                "time_in_queue_secs":   0,
                "divergence_bar_time":  signal.get("divergence_bar_time"),
            }
            return {"type": "lifecycle_batch", "events": [_filled_evt, {"type": "signal", **signal}]}
        return {"type": "signal", **signal}

    # ── IDLE ──────────────────────────────────────────────────────────────────
    if context.tdo is None:
        return None
    if ts < min_signal_ts:
        return None
    if _in_blackout(ts.time()):
        return None
    if bar_idx < 3:
        return None

    # Pre-extract current bar values as floats once — used by detect_smt_divergence
    # and the EXPANDED_REFERENCE_LEVELS fallback below.
    _cur_mes_h = float(mes_highs[bar_idx]) if mes_highs is not None else float(mes_reset.iloc[bar_idx]["High"])
    _cur_mes_l = float(mes_lows[bar_idx])  if mes_lows  is not None else float(mes_reset.iloc[bar_idx]["Low"])
    _cur_mes_c = float(mes_closes[bar_idx]) if mes_closes is not None else float(mes_reset.iloc[bar_idx]["Close"])
    _cur_mnq_h = float(mnq_highs[bar_idx])
    _cur_mnq_l = float(mnq_lows[bar_idx])
    _cur_mnq_c = float(mnq_closes[bar_idx])
    _cur_vals_dict = {
        "mes_h": _cur_mes_h, "mes_l": _cur_mes_l, "mes_c": _cur_mes_c,
        "mnq_h": _cur_mnq_h, "mnq_l": _cur_mnq_l, "mnq_c": _cur_mnq_c,
    }
    _smt = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0, _cached=smt_cache, _cur_vals=_cur_vals_dict)

    # Expanded reference levels: check prev-day/week extremes
    if _smt is None and EXPANDED_REFERENCE_LEVELS and context.ref_lvls:
        _cur_mes_b = mes_reset.iloc[bar_idx]
        _cur_mnq_b = mnq_reset.iloc[bar_idx]
        _best_ref = None
        for _rk in (
            ("prev_day_mes_high", "prev_day_mes_low", "prev_day_mnq_high", "prev_day_mnq_low", False),
            ("prev_session_mes_high", "prev_session_mes_low", "prev_session_mnq_high", "prev_session_mnq_low", False),
            ("week_mes_high", "week_mes_low", "week_mnq_high", "week_mnq_low", False),
            ("prev_day_mes_close_high", "prev_day_mes_close_low", "prev_day_mnq_close_high", "prev_day_mnq_close_low", True),
            ("prev_session_mes_close_high", "prev_session_mes_close_low", "prev_session_mnq_close_high", "prev_session_mnq_close_low", True),
            ("week_mes_close_high", "week_mes_close_low", "week_mnq_close_high", "week_mnq_close_low", True),
        ):
            _mh, _ml, _nh, _nl, _uc = _rk
            if _uc and not HIDDEN_SMT_ENABLED:
                continue
            _cand = _check_smt_against_ref(
                _cur_mes_b, _cur_mnq_b,
                context.ref_lvls.get(_mh), context.ref_lvls.get(_ml),
                context.ref_lvls.get(_nh), context.ref_lvls.get(_nl),
                use_close=_uc,
            )
            if _cand is not None and (_best_ref is None or _cand[1] > _best_ref[1]):
                _best_ref = _cand
        if _best_ref is not None:
            _smt = _best_ref

    _smt_fill = None
    _displacement_dir = None
    if _smt is None:
        if SMT_FILL_ENABLED:
            _smt_fill = detect_smt_fill(mes_reset, mnq_reset, bar_idx)
        if _smt_fill is None and SMT_OPTIONAL:
            for _d in ("short", "long"):
                if TRADE_DIRECTION in ("both", _d) and detect_displacement(mnq_reset, bar_idx, _d):
                    _displacement_dir = _d
                    break

    if _smt is None and _smt_fill is None and _displacement_dir is None:
        return None

    if _smt is not None:
        direction, _smt_sweep, _smt_miss, _smt_type, _smt_defended = _smt
    elif _smt_fill is not None:
        direction, _, _ = _smt_fill
        _smt_sweep = _smt_miss = 0.0; _smt_type = "fill"; _smt_defended = None
    else:
        direction = _displacement_dir
        _smt_sweep = _smt_miss = 0.0; _smt_type = "displacement"; _smt_defended = None

    if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
        return None

    # HTF visibility filter — also collect which TFs confirmed (stored for signal annotation)
    if HTF_VISIBILITY_REQUIRED and state.htf_state:
        _htf_confirmed: list = []
        for _T, _hs in state.htf_state.items():
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
            return None
        _htf_confirmed_tfs: "list | None" = _htf_confirmed
    else:
        _htf_confirmed_tfs = None

    _displacement_bar_extreme = None
    if _smt_type == "displacement":
        _displacement_bar_extreme = (
            float(mnq_lows[bar_idx]) if direction == "long" else float(mnq_highs[bar_idx])
        )

    if _smt_type == "displacement" and MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT > 0:
        _score = context.hyp_ctx.get("hypothesis_score", 0) if context.hyp_ctx else 0
        if _score < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT:
            return None

    fvg_lookback = max(3, round(1200 / context.bar_seconds))
    _pending_fvg = detect_fvg(mnq_reset, bar_idx, direction, lookback=fvg_lookback)
    _raw_fvg_present = _pending_fvg is not None

    if FVG_LAYER_B_REQUIRES_HYPOTHESIS:
        _score = context.hyp_ctx.get("hypothesis_score", 0) if context.hyp_ctx else 0
        if _score < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT:
            _pending_fvg = None

    if OVERNIGHT_SWEEP_REQUIRED:
        oh = context.overnight.get("overnight_high")
        ol = context.overnight.get("overnight_low")
        if direction == "short" and oh is not None:
            if run_ses_high <= oh:
                return None
        if direction == "long" and ol is not None:
            if run_ses_low >= ol:
                return None

    if SILVER_BULLET_WINDOW_ONLY and not _in_silver_bullet(ts.time()):
        return None

    ac = find_anchor_close(mnq_reset, bar_idx, direction)
    if ac is None:
        return None

    state.pending_direction               = direction
    state.anchor_close                    = ac
    state.pending_smt_sweep               = _smt_sweep
    state.pending_smt_miss                = _smt_miss
    state.divergence_bar_idx              = bar_idx
    state.pending_div_bar_high            = float(mnq_highs[bar_idx])
    state.pending_div_bar_low             = float(mnq_lows[bar_idx])
    state.pending_smt_defended            = _smt_defended
    state.pending_smt_type                = _smt_type
    state.pending_fvg_zone                = _pending_fvg
    state.pending_fvg_detected            = _raw_fvg_present
    state.pending_displacement_bar_extreme = _displacement_bar_extreme
    state.pending_htf_confirmed_tfs       = _htf_confirmed_tfs
    state.scan_state                      = "WAITING_FOR_ENTRY"
    state.conf_window_start               = bar_idx + 1
    state.pending_div_score = divergence_score(
        _smt_sweep, _smt_miss,
        body_pts=abs(float(mnq_closes[bar_idx]) - float(mnq_opens[bar_idx])),
        smt_type=_smt_type,
        hypothesis_direction=context.hyp_dir,
        div_direction=direction,
    )
    if MIN_DIV_SCORE > 0 and state.pending_div_score < MIN_DIV_SCORE:
        state.scan_state = "IDLE"
        state.pending_direction = None
        state.anchor_close = None
        return None
    state.pending_div_provisional   = (_smt_type == "displacement")
    state.pending_discovery_bar_idx = bar_idx
    state.pending_discovery_price   = float(mnq_closes[bar_idx])
    # Emit LIMIT_PLACED when operating in limit mode
    if LIMIT_ENTRY_BUFFER_PTS is not None:
        _prelim = _build_preliminary_limit_signal(
            state, context, ts, run_ses_high, run_ses_low
        )
        if _prelim is not None:
            state.last_limit_signal_snapshot = dict(_prelim)
            state.last_limit_move_bar_idx    = bar_idx
            return {"type": EVT_LIMIT_PLACED, "signal": _prelim}
    return None


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
    # Cache frequently accessed bar values as locals to avoid repeated dict/attr lookups.
    _bar_high  = current_bar["High"]
    _bar_low   = current_bar["Low"]
    _bar_close = current_bar["Close"]
    _bar_open  = current_bar["Open"]

    direction   = position["direction"]
    entry_price = position["entry_price"]
    tp          = position["take_profit"]

    # Track the stop at bar start so human-mode can surface a MOVE_STOP event
    # when any path below (breakeven, trail-after-TP, layer-B) tightens the stop.
    _orig_stop = position["stop_price"]

    # ── Trail-after-TP: disabled when a secondary target exists (secondary takes over) ──
    if TRAIL_AFTER_TP_PTS > 0 and position.get("secondary_target") is None:
        if position.get("tp_breached"):
            # Already past TDO — update trailing stop each bar
            if direction == "short":
                best = min(position.get("best_after_tp", tp), _bar_low)
                position["best_after_tp"] = best
                activation_dist = TRAIL_ACTIVATION_R * position.get("initial_stop_pts", 0.0)
                if tp - best >= activation_dist:
                    new_stop = best + TRAIL_AFTER_TP_PTS
                    position["stop_price"] = min(position["stop_price"], new_stop)  # never widen
            else:
                best = max(position.get("best_after_tp", tp), _bar_high)
                position["best_after_tp"] = best
                activation_dist = TRAIL_ACTIVATION_R * position.get("initial_stop_pts", 0.0)
                if best - tp >= activation_dist:
                    new_stop = best - TRAIL_AFTER_TP_PTS
                    position["stop_price"] = max(position["stop_price"], new_stop)  # never widen
        else:
            # Check if TDO was crossed this bar for the first time
            crossed = (direction == "short" and _bar_low  <= tp) or \
                      (direction == "long"  and _bar_high >= tp)
            if crossed:
                # Check original stop before marking breach — a bar can cross both TDO and stop.
                stop = position["stop_price"]
                if direction == "short" and float(_bar_high) >= stop:
                    return "exit_stop"
                if direction == "long"  and float(_bar_low)  <= stop:
                    return "exit_stop"
                position["tp_breached"] = True
                if direction == "short":
                    position["best_after_tp"] = min(tp, _bar_low)
                else:
                    position["best_after_tp"] = max(tp, _bar_high)
                return "hold"

    # ── Set tp_breached on first primary TP crossing (for secondary target tracking) ──
    # Runs when trail is disabled or secondary_target is set (trail block above is skipped in both cases).
    if not position.get("tp_breached"):
        if direction == "long"  and float(_bar_high) >= tp:
            position["tp_breached"] = True
        elif direction == "short" and float(_bar_low)  <= tp:
            position["tp_breached"] = True

    # ── Secondary target check: exit after primary breach ────────────────────
    sec = position.get("secondary_target")
    if sec is not None and position.get("tp_breached"):
        if direction == "long"  and float(_bar_high) >= sec:
            return "exit_secondary"
        if direction == "short" and float(_bar_low)  <= sec:
            return "exit_secondary"

    # ── Layer B entry (FVG retracement) ─────────────────────────────────────
    if TWO_LAYER_POSITION and FVG_LAYER_B_TRIGGER:
        if not position.get("layer_b_entered") and not position.get("partial_done"):
            fvg_high = position.get("fvg_high")
            fvg_low  = position.get("fvg_low")
            if fvg_high is not None and fvg_low is not None:
                in_fvg = (
                    (direction == "long"  and _bar_low  <= fvg_high and _bar_low  >= fvg_low) or
                    (direction == "short" and _bar_high >= fvg_low  and _bar_high <= fvg_high)
                )
                if in_fvg:
                    total_target = position.get("total_contracts_target", position["contracts"])
                    layer_b_contracts = total_target - position["contracts"]
                    if layer_b_contracts > 0:
                        lb_entry = float(_bar_close)
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
                progress = (entry_price - _bar_low) / tdo_dist
            else:
                progress = (_bar_high - entry_price) / tdo_dist
            if progress >= BREAKEVEN_TRIGGER_PCT:
                # Only tighten the stop, never widen it
                if direction == "short":
                    position["stop_price"] = min(position["stop_price"], entry_price)
                else:
                    position["stop_price"] = max(position["stop_price"], entry_price)
                position["breakeven_active"] = True

    # ── Deception exit: opposing displacement candle ─────────────────────────
    # A strong candle in the opposite direction of the trade (body >= MIN_DISPLACEMENT_PTS)
    # signals the thesis is likely wrong. Exits at bar close as a market order.
    # Inlined (not via detect_displacement()) so the check runs regardless of SMT_OPTIONAL,
    # which detect_displacement gates on and which governs an unrelated entry path.
    if DECEPTION_OPPOSING_DISP_EXIT and MIN_DISPLACEMENT_PTS > 0:
        body_pts = abs(float(_bar_close) - float(_bar_open))
        if body_pts >= MIN_DISPLACEMENT_PTS:
            opposing_candle = (
                (direction == "short" and _bar_close > _bar_open) or
                (direction == "long"  and _bar_close < _bar_open)
            )
            if opposing_candle:
                return "exit_invalidation_opposing_disp"

    # ── Thesis-invalidation exits (close-based; fire before stop check) ─────────
    # MSS: divergence extreme breached on a closing basis.
    if INVALIDATION_MSS_EXIT:
        div_low  = position.get("divergence_bar_low")
        div_high = position.get("divergence_bar_high")
        if direction == "long" and div_low is not None and _bar_close < div_low:
            return "exit_invalidation_mss"
        if direction == "short" and div_high is not None and _bar_close > div_high:
            return "exit_invalidation_mss"

    # CISD: midnight open breached on a closing basis.
    if INVALIDATION_CISD_EXIT:
        mo = position.get("midnight_open")
        if mo is not None:
            if direction == "long"  and _bar_close < mo:
                return "exit_invalidation_cisd"
            if direction == "short" and _bar_close > mo:
                return "exit_invalidation_cisd"

    # SMT: MNQ defended level breached on a closing basis.
    if INVALIDATION_SMT_EXIT:
        defended = position.get("smt_defended_level")
        if defended is not None:
            if direction == "long"  and _bar_close < defended:
                return "exit_invalidation_smt"
            if direction == "short" and _bar_close > defended:
                return "exit_invalidation_smt"

    stop = position["stop_price"]

    # ── Partial exit at first draw on liquidity ─────────────────────────────
    if PARTIAL_EXIT_ENABLED and not position.get("partial_done"):
        lvl = position.get("partial_exit_level")
        if lvl is not None:
            if direction == "long"  and _bar_high >= lvl:
                position["partial_done"]  = True
                position["partial_price"] = lvl
                return "partial_exit"
            if direction == "short" and _bar_low  <= lvl:
                position["partial_done"]  = True
                position["partial_price"] = lvl
                return "partial_exit"

    # ── Exit checks ───────────────────────────────────────────────────────────
    # exit_tp is only used when trail-after-TP is disabled; otherwise the stop
    # takes over once TDO is breached (handled in the block above).
    if direction == "long":
        if _bar_low  <= stop:                                                         return "exit_stop"
        if TRAIL_AFTER_TP_PTS == 0 and position.get("secondary_target") is None and _bar_high >= tp: return "exit_tp"
    else:  # short
        if _bar_high >= stop:                                                         return "exit_stop"
        if TRAIL_AFTER_TP_PTS == 0 and position.get("secondary_target") is None and _bar_low  <= tp: return "exit_tp"
    # Human mode: surface stop mutations as an explicit MOVE_STOP event so downstream
    # consumers (live signal emitter, backtest record) can act on the change rather
    # than silently inheriting the mutated position["stop_price"].
    if HUMAN_EXECUTION_MODE and position["stop_price"] != _orig_stop:
        position["_pending_move_stop"] = position["stop_price"]
        return "move_stop"
    return "hold"
