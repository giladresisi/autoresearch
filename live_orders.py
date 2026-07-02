# live_orders.py
# Singleton executor for live order routing (V2 pipeline + ad-hoc human orders).
# Executor is chosen once at import time from LIVE_TRADING env var:
#   LIVE_TRADING=true  → PickMyTradeExecutor (real PMT webhook)
#   LIVE_TRADING=false → SimulatedBrokerExecutor (no-op / paper mode)
from __future__ import annotations

import datetime
import json
import os
import zoneinfo
from pathlib import Path

import pandas as pd

import smt_state

from dotenv import load_dotenv

import paths
import stop_utils
load_dotenv()

_LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"
# Shadow-live: DISCONNECTED=true runs the strategy fully in resumed mode but suppresses every
# send to the PMT webhook (execution/pickmytrade._post_order) AND every broker reconcile/read
# below — so the broker stays completely silent while we record what live WOULD have done, for a
# retrospective compare vs the regression. Read once at import → fixed for the orchestrator's
# lifetime (toggling .env mid-run has no effect until the next restart).
_DISCONNECTED = os.getenv("DISCONNECTED", "false").strip().lower() in ("1", "true", "yes", "on")

if _LIVE:
    from execution.pickmytrade import PickMyTradeExecutor
    if _DISCONNECTED:
        print(
            "[PMT] DISCONNECTED mode ON — strategy runs resumed but NO orders are sent to the "
            "broker and broker reconcile/reads are skipped (shadow-live; compare vs regression)",
            flush=True,
        )
    _account_ids = [s.strip() for s in os.environ.get("TRADING_ACCOUNT_IDS", "").split(",") if s.strip()]
    _executor = PickMyTradeExecutor(
        webhook_url=os.environ["PMT_WEBHOOK_URL"],
        api_key=os.environ["PMT_API_KEY"],
        symbol=os.environ.get("TRADING_SYMBOL", "MNQ1!"),
        account_ids=_account_ids,
        contracts=int(os.environ.get("TRADING_CONTRACTS", "2")),
        entry_slip_ticks=int(os.environ.get("PMT_ENTRY_SLIP_TICKS", "2")),
        disconnected=_DISCONNECTED,
    )
else:
    from execution.simulated import SimulatedBrokerExecutor
    _executor = SimulatedBrokerExecutor(human_mode=True)

_ET = zoneinfo.ZoneInfo("America/New_York")

# Session date locked at startup by automation.main (ET date, YYYY-MM-DD).
# Never recalculated mid-session so the folder stays stable across ET midnight.
_SESSION_DATE: str = ""

# D4: timestamp when last stop entry was dispatched (bar time)
_entry_sent_bar_time: "pd.Timestamp | None" = None

# D6: timestamp when last stop entry fill was detected (bar time)
_fill_bar_time: "pd.Timestamp | None" = None

# D8: timestamp when last stop-entry-cancelled fired (bar time), and pending close flag
_cancel_bar_time: "pd.Timestamp | None" = None
_pending_close_after: "pd.Timestamp | None" = None

# Manual entry pause (trade.py pause/resume). Canonical flag path + is_paused() live in
# smt_state so this dispatch gate, the live pipeline's entry gate, and trade.py share one
# source. Resolved at call time via smt_state.pause_path() (the session folder is not known
# at import), so pause()/resume() below call it rather than capturing a frozen constant.

# Live-execution safety net mirroring strategy.MKT_FILL_MIN_STOP_DISTANCE: a stop entry the
# executor downgrades to MKT fills at market, which can land closer to the protective stop than
# planned — guarantee at least this many points between the fill and the stop so a downgrade fill
# (or any caller) is never sent with a near-zero-risk stop (incident 2026-06-04). The strategy
# already floors this on the backtested path; this is defence-in-depth at the dispatch boundary.
# Keep in sync with strategy.MKT_FILL_MIN_STOP_DISTANCE. Live-only: never reached in backtest.
_MIN_FILL_STOP_DISTANCE = 10.0


def _floor_stop_distance(direction: str, entry_price: float, stop_price: float) -> float:
    """Widen `stop_price` so it is at least _MIN_FILL_STOP_DISTANCE from `entry_price`.

    Only ever widens (never tightens a structurally-farther stop). Mirrors the strategy's
    market-fill floor so a downgrade-filled stop entry can't be left with a near-zero stop.
    """
    if direction == "long" and (entry_price - stop_price) < _MIN_FILL_STOP_DISTANCE:
        return entry_price - _MIN_FILL_STOP_DISTANCE
    if direction == "short" and (stop_price - entry_price) < _MIN_FILL_STOP_DISTANCE:
        return entry_price + _MIN_FILL_STOP_DISTANCE
    return stop_price


# ---------------------------------------------------------------------------
# Broker reconciliation hook (GIL-36) — live-only
# ---------------------------------------------------------------------------
# A persistent READ-ONLY Tradovate orders-blotter reader, started lazily on the first live
# entry. After each entry that carries a protective stop we spawn a daemon-thread verify that
# reads the broker blotter and corrects a rejected SL leg / rejected entry. Detection-only
# browser; all correction goes back through PMT (update_stop_loss) / position.json. Guarded so
# it is a no-op in backtest/simulated mode and can NEVER raise into the trading loop.
_recon_reader = None
_recon_reader_started = False


def _get_recon_reader():
    """Lazily start the shared read-only reader (live mode only). Returns None on any failure."""
    global _recon_reader, _recon_reader_started
    if not _LIVE or _DISCONNECTED:
        # DISCONNECTED: broker is silent (no orders sent) → nothing to read/reconcile.
        return None
    if _recon_reader_started:
        return _recon_reader
    _recon_reader_started = True
    try:
        from broker_recon.reader import TradovateOrderReader
        r = TradovateOrderReader()
        if r.start():
            _recon_reader = r
    except Exception as exc:  # pragma: no cover - reader is best-effort
        print(f"[live_orders] reconcile reader start failed (ignored): {exc}", flush=True)
        _recon_reader = None
    return _recon_reader


def _spawn_reconcile(direction: str, intended_entry: float, intended_stop: float,
                     fill: float) -> None:
    """Spawn the post-entry broker reconciliation on a daemon thread (live-only, best-effort).

    Never raises into the caller; a no-op outside live mode or when the reader is unavailable.
    """
    if not _LIVE or _DISCONNECTED:
        # DISCONNECTED: no order was sent to the broker → nothing to reconcile against.
        return
    try:
        reader = _get_recon_reader()
        if reader is None:
            return
        from broker_recon import reconcile as _recon
        entry = {
            "direction": direction,
            "intended_entry": float(intended_entry),
            "intended_stop": float(intended_stop),
            "symbol": os.environ.get("TRADING_SYMBOL", "MNQ1!"),
            "time": _now_et(),
        }
        _recon.reconcile_after_entry(entry, float(fill), reader=reader)
    except Exception as exc:  # pragma: no cover - reconciliation must never break a trade
        print(f"[live_orders] reconcile spawn failed (ignored): {exc}", flush=True)


# ---------------------------------------------------------------------------
# Reconcile-on-close (GIL-42) — live-only, SYNCHRONOUS
# ---------------------------------------------------------------------------
# A bar-assumed phantom close (the IB feed printed a level Tradovate never traded through)
# marks the strategy flat while the broker is still in the position, then a re-entry STACKS
# on the live lot and the FIFO stop match realizes an outsized loss (verified -$428 on
# 2026-06-22). Before every close we fetch a detection-only broker snapshot and either ADOPT
# the live position (phantom close), flatten state without an order (broker already flat), or
# resize. Synchronous because the post-close cooldown / re-entry happens on the SAME or NEXT
# bar in the same thread — running inline guarantees the broker is reconciled before any entry
# fires. The fetch is bounded (short Playwright timeout) and fails safe: any failure returns
# False so the close proceeds as the strategy intended. No-op outside live mode.

def _reconcile_on_close(close_event: dict) -> bool:
    """Live-only synchronous close reconcile. Returns True iff the pending broker
    close must be SUPPRESSED. No-op (returns False) outside live mode or on any failure."""
    if not _LIVE or _DISCONNECTED:
        # DISCONNECTED: no close was sent to the broker and the broker holds no position we
        # placed → there is nothing to reconcile; let the strategy's intended close stand.
        return False
    try:
        from broker_recon import broker_state, recon_on_close
        symbol = os.environ.get("TRADING_SYMBOL", "MNQ1!")
        broker = broker_state.fetch_broker_state(symbol)   # None on failure -> noop
        strat_active = _load_pos().get("active") or {}
        decision = recon_on_close.decide_correction(strat_active, broker)
        return recon_on_close.apply_correction(decision, intended_close_event=close_event)
    except Exception as exc:  # pragma: no cover - reconcile must never break a close
        print(f"[live_orders] close reconcile failed (ignored): {exc}", flush=True)
        return False


def set_session_date(d: str) -> None:
    global _SESSION_DATE
    _SESSION_DATE = d


def _now_et() -> str:
    return datetime.datetime.now(_ET).isoformat()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(event: dict) -> None:
    """Append one JSON line to sessions/{today}/events.jsonl.

    kind and time are always written first, followed by direction when present;
    remaining fields follow alphabetically.
    """
    today = _SESSION_DATE or datetime.datetime.now(_ET).date().isoformat()
    path = paths.sessions_dir() / today / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered: dict = {"kind": event.get("kind", ""), "time": event.get("time", "")}
    if "direction" in event:
        ordered["direction"] = event["direction"]
    ordered.update(sorted((k, v) for k, v in event.items() if k not in ("kind", "time", "direction")))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ordered) + "\n")


# ---------------------------------------------------------------------------
# Position state helpers
# ---------------------------------------------------------------------------

def _load_pos() -> dict:
    # Cross-process safety: a standalone caller (trade.py, ad-hoc REPL) that never set
    # the state dir must operate on the SAME session position.json as the orchestrator —
    # not the legacy worktree-local data/ (2026-06-05 04:21 phantom fill: a manual close
    # cleared stop_entry in the wrong file). No-op once the state dir is set.
    smt_state.ensure_live_state_dir()
    from smt_state import load_position
    return load_position()


def _save_pos(pos: dict) -> None:
    smt_state.ensure_live_state_dir()
    from smt_state import save_position
    save_position(pos)


def get_position() -> dict:
    """Return current position.json state."""
    return _load_pos()


def has_active_position() -> bool:
    """True if position.json shows an active (filled) trade."""
    return bool(_load_pos().get("active"))


def has_pending_entry() -> bool:
    """True if position.json shows an unfilled stop entry order."""
    return bool(_load_pos().get("stop_entry"))


def is_paused() -> bool:
    """True if a manual entry pause is in effect (new automatic entries suppressed)."""
    return smt_state.is_paused()


def pause() -> bool:
    """Engage the manual entry pause. Idempotent: returns False (no-op) if already paused."""
    flag = smt_state.pause_path()
    if flag.exists():
        return False
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(_now_et(), encoding="utf-8")
    _log({"kind": "paused", "time": _now_et()})
    return True


def resume() -> bool:
    """Lift the manual entry pause. Idempotent: returns False (no-op) if not paused."""
    flag = smt_state.pause_path()
    if not flag.exists():
        return False
    flag.unlink()
    _log({"kind": "resumed", "time": _now_et()})
    return True


# ---------------------------------------------------------------------------
# Unified API — each function: log → executor → sync position.json
# ---------------------------------------------------------------------------

def place_stop_entry(direction: str, entry_price: float, stop_price: float, *, source: str = "strategy") -> None:
    """Place unfilled stop entry. Logs, dispatches STP order, writes stop_entry to position.json.

    If the executor downgrades the STP to a market order (entry within 5 pts of the
    market price), the broker fills immediately. In that case we record the fill in
    strategy state right away (see _register_downgraded_fill) instead of leaving a
    pending stop_entry — otherwise the bar-based fill detector might never confirm it
    (price downgrade-filled below the entry level and may never reach it on a later bar),
    leaving the broker long while position.json shows flat. See incident 2026-06-04.
    """
    now = _now_et()
    # Safety net: guarantee the protective stop clears _MIN_FILL_STOP_DISTANCE from the entry
    # before the order (and its broker SL) is sent — a downgrade fill must never rest on a
    # near-zero-risk stop (incident 2026-06-04). Only widens; far stops are untouched.
    stop_price = _floor_stop_distance(direction, entry_price, stop_price)
    _bar_high, _bar_low = _current_bar_extremes()
    pmt_signal = {
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_fill_bars": 1,
        "current_price": _current_price(),
        # Bar extremes so the executor can downgrade STP->MKT when the live market has
        # already touched the trigger intrabar (not just on the lagging close).
        "bar_high": _bar_high,
        "bar_low": _bar_low,
    }
    rec = _executor.place_entry(pmt_signal, None)
    entry_live = getattr(_executor, "_entry_is_live", True)
    if entry_live and getattr(rec, "order_type", None) == "market":
        # Record the executor's market-anchored fill estimate (current price + slip),
        # NOT the trigger — the market already passed the trigger, so the broker fills
        # at the market (live gaps were 12-20 pts in fast moves; 2026-06-05 05:17).
        _fill = float(getattr(rec, "fill_price", 0.0) or 0.0)
        if _fill <= 0.0:
            _fill = entry_price  # defensive: executor gave no estimate
        # Preventive re-anchor (GIL-36): the STP->MKT fill (`_fill`) lands at the market,
        # which on a downgrade is PAST the trigger — so a stop that looked valid vs the
        # trigger `entry_price` at :192 can now sit on the WRONG side of the actual fill
        # (above a long fill / below a short fill). Tradovate then rejects that SL leg,
        # leaving the entry naked (incident 2026-06-17 10:11). Run the stop through the
        # single-source-of-truth helper against the assumed fill; it returns the stop
        # UNCHANGED when already valid, so only the wrong-side/too-close edge changes.
        safe_stop = stop_utils.valid_stop_for_fill(direction, _fill, stop_price, entry_price)
        if safe_stop != stop_price:
            stop_price = safe_stop
            # The original (wrong-side) SL was already embedded in the MKT order sent at
            # :205. Re-send the corrected SL immediately so the broker's working stop
            # matches the re-anchored value before _register_downgraded_fill records it
            # into position.json active.stop.
            _executor.update_stop_loss(
                {"direction": direction, "stop_price": stop_price}, None)
        _register_downgraded_fill(direction, entry_price, stop_price, source, now,
                                  fill_price=_fill)
        # Broker reconciliation (GIL-36): verify the SL leg actually rests at the broker on
        # this STP->MKT downgrade fill — the exact ex1 case. intended_stop is the post-
        # preventive stop_price; intended_entry is the trigger; fill is the assumed fill.
        # GIL-42 note: NO close-reconcile (_reconcile_on_close) call here — the STP->MKT
        # downgrade is an ENTRY, not a close, and is only reached when the strategy believed
        # it was flat; the close that made it flat already ran the synchronous close-reconcile
        # in dispatch. The GIL-36 after-entry verify below remains the right tool here.
        _spawn_reconcile(direction, entry_price, stop_price, _fill)
        return
    pos = _load_pos()
    pos["stop_entry"] = str(entry_price)
    pos["stop_direction"] = "up" if direction == "long" else "down"
    pos["pending_stop"] = stop_price
    pos["stop_entry_source"] = source
    if not entry_live:
        pos["stop_entry_unplaced"] = True
    else:
        pos.pop("stop_entry_unplaced", None)
    _save_pos(pos)
    _log({"kind": "new-stop-entry", "time": now, "direction": direction,
          "entry_price": entry_price, "stop_price": stop_price})


def _register_downgraded_fill(direction: str, entry_price: float, stop_price: float,
                              source: str, now: str, fill_price: float | None = None) -> None:
    """Record an immediate fill after the executor downgraded an STP entry to MKT.

    Mirrors the strategy's stop-entry fill transition (strategy.py): set active, clear
    the pending stop entry, re-anchor the cautious ladder to the fill price (Addendum 4),
    and log stop-entry-filled — all at dispatch time, since the broker already filled.

    fill_price is the executor's market-anchored estimate of the actual fill (current
    price + slip ticks); entry_price stays the original trigger. The protective stop is
    NOT re-anchored to the fill — it was already sent to the broker at placement, and
    strategy state must keep matching the broker's working SL.
    """
    fill = float(fill_price) if fill_price else float(entry_price)
    pos = _load_pos()
    pos["active"] = {
        "time": now,
        "fill_price": fill,
        "direction": direction,
        "stop": stop_price,
        "contracts": 2,
        "cautious": "no",
        "source": source,
    }
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos.pop("stop_entry_source", None)
    pos.pop("stop_entry_unplaced", None)
    _save_pos(pos)
    _recompute_cautious_at_fill(fill)
    # SMT-v2 Phase 1: freeze the fill-anchored (post-recompute) management direction +
    # cautious ladder into active — immutable for the life of the trade. Reload the
    # hypothesis so the frozen ladder reflects the re-anchored (post-recompute) values.
    hyp = smt_state.load_hypothesis()
    smt_state.freeze_active_mgmt(pos["active"], direction, hyp)
    _save_pos(pos)
    # Align events.jsonl with signals.log. The strategy emitted a `new-stop-entry`, but the
    # broker instantly downgraded it STP->MKT and filled the same instant — so on this path
    # events.jsonl would otherwise show a standalone `stop-entry-filled` with no originating
    # `new-stop-entry` (signals.log has the placement; events.jsonl did not). Log the
    # placement too, then the fill, both tagged `stp_mkt_downgrade` so the immediate-downgrade
    # origin is explicit (and so the fill is distinguishable from a later bar-based fill).
    _log({"kind": "new-stop-entry", "time": now, "direction": direction,
          "entry_price": entry_price, "stop_price": stop_price, "stp_mkt_downgrade": True})
    _log({"kind": "stop-entry-filled", "time": now, "direction": direction,
          "price": fill, "stop_price": stop_price, "trigger_price": entry_price,
          "stp_mkt_downgrade": True})


def _recompute_cautious_at_fill(fill_price: float) -> None:
    """Re-anchor the cautious ladder in hypothesis.json to the actual fill price.

    The new-stop-entry path never recomputes cautious (only the strategy's own fill
    paths do), so on an immediate downgrade-fill we must do it here. Best-effort: a
    failure must not block the fill from being recorded.
    """
    try:
        import smt_state
        import hypothesis as _hyp_mod
        hyp = smt_state.load_hypothesis()
        if not hyp:
            return
        dly = smt_state.load_daily() or {}
        glb = smt_state.load_global() or {}
        _hyp_mod.recompute_cautious_for_fill(
            hyp, fill_price, dly.get("liquidities", []), glb.get("all_time_high"))
        smt_state.save_hypothesis(hyp)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[live_orders] cautious recompute after downgrade fill failed: {exc}", flush=True)


def _current_price() -> float:
    """Freshest available estimate of the current market price.

    Order (first FRESH source wins; freshness = last bar within 3 minutes of now):
    1. The in-process live 1m frame (strategy_smt's module bars, updated by the IB feed
       on every completed 1m bar) — no disk IO, <=60s stale in the orchestrator process.
    2. The live MNQ_1m.parquet last close (rewritten by the orchestrator every completed
       1m bar) — covers standalone processes (trade.py, ad-hoc REPLs).
    3. Legacy fallbacks, unguarded: bar_state midpoint (potential stops of the PREVIOUS
       5m block — up to ~5m stale; this staleness was the source of the 12-20pt STP->MKT
       fill gaps and wrong downgrade decisions, 2026-06-05 05:17 note), then the most
       recent parquet close regardless of age (manual ops outside market hours).
    """
    _now = pd.Timestamp.now(tz=_ET)
    _fresh_cutoff = _now - pd.Timedelta(minutes=3)

    # 1) In-process live bars (orchestrator process only; empty elsewhere). The freshness
    # guard also keeps leftover backtest frames or a stale restart from leaking in.
    try:
        import strategy_smt as _ss
        _bars = getattr(_ss, "_mnq_bars", None)
        if _bars is not None and len(_bars) and _bars.index.tz is not None \
                and _bars.index[-1] >= _fresh_cutoff:
            return float(_bars["Close"].iloc[-1])
    except Exception:
        pass

    # 2) Live 1m parquet, fresh (cross-process: the orchestrator rewrites it every minute).
    try:
        _p = paths.general_live_dir() / "MNQ_1m.parquet"
        if _p.exists():
            _df = pd.read_parquet(_p, columns=["Close"])
            if not _df.empty and _df.index.tz is not None and _df.index[-1] >= _fresh_cutoff:
                return float(_df["Close"].iloc[-1])
    except Exception:
        pass

    # 3) Legacy: bar_state midpoint, then any parquet close regardless of age.
    from smt_state import load_bar_state
    bar = load_bar_state()
    if bar and "potential_stop_long" in bar and "potential_stop_short" in bar:
        return (float(bar["potential_stop_long"]) + float(bar["potential_stop_short"])) / 2.0
    for _name in ("MNQ_1s.parquet", "MNQ_1m.parquet"):
        try:
            _p = paths.general_live_dir() / _name
            if _p.exists():
                _df = pd.read_parquet(_p, columns=["Close"])
                if not _df.empty:
                    return float(_df["Close"].iloc[-1])
        except Exception:
            continue
    return 0.0


def _current_bar_extremes() -> "tuple[float, float]":
    """Freshest (high, low) of the most recent live bar — same source priority as
    `_current_price`, but the bar EXTREMES rather than the close.

    Used by the STP->MKT downgrade so a stop whose trigger the live market has already
    TOUCHED intrabar (the bar high reached a long trigger / the bar low reached a short
    trigger) is sent MKT instead of a resting STP that Tradovate rejects as at/past market
    (2026-06-11 — four rejected brackets). The close lags within the bar, so the close-only
    check missed these. Returns (0.0, 0.0) when no fresh source is available (the executor
    then falls back to the close-only behavior).
    """
    _now = pd.Timestamp.now(tz=_ET)
    _fresh_cutoff = _now - pd.Timedelta(minutes=3)
    try:
        import strategy_smt as _ss
        _bars = getattr(_ss, "_mnq_bars", None)
        if _bars is not None and len(_bars) and _bars.index.tz is not None \
                and _bars.index[-1] >= _fresh_cutoff:
            return float(_bars["High"].iloc[-1]), float(_bars["Low"].iloc[-1])
    except Exception:
        pass
    try:
        _p = paths.general_live_dir() / "MNQ_1m.parquet"
        if _p.exists():
            _df = pd.read_parquet(_p, columns=["High", "Low"])
            if not _df.empty and _df.index.tz is not None and _df.index[-1] >= _fresh_cutoff:
                return float(_df["High"].iloc[-1]), float(_df["Low"].iloc[-1])
    except Exception:
        pass
    return 0.0, 0.0


def place_market_entry(direction: str, entry_price: float, stop_price: float, *, flatten_first: bool = False, source: str = "strategy") -> None:
    """Enter at market with stop. Logs, dispatches MKT+sl, writes active to position.json."""
    now = _now_et()
    # Safety net (mirrors place_stop_entry): anchor the protective stop to the assumed fill
    # and guarantee it clears _MIN_FILL_STOP_DISTANCE on the protective side BEFORE the order
    # is sent. A market fill must never carry a stop at/beyond the fill — Tradovate rejects an
    # invalid stop leg (long stop ≥ fill / short stop ≤ fill), leaving the entry naked — nor a
    # near-zero stop that the entry bar instantly trips (incident 2026-06-04: manual `trade.py
    # up` filled then immediately stopped out / had its S/L leg rejected). The fill anchor is
    # entry_price when given, else the current market price (manual entries pass entry 0.0);
    # anchoring to 0.0 would defeat the floor. Only widens; far stops are untouched.
    fill_price = entry_price if entry_price != 0.0 else _current_price()
    # Preventive re-anchor (GIL-36): the anchor `fill_price` IS the assumed market fill, so
    # run the stop through the single-source-of-truth helper to guarantee it rests on the
    # correct side of the fill at the intended risk (not merely the right DISTANCE, which is
    # all _floor_stop_distance checked). `intended_entry` is the original trigger when given,
    # else the fill (manual market entries pass entry 0.0 → risk floors to MIN_STOP_DISTANCE,
    # matching today's floor behavior). Returns the stop unchanged whenever already valid.
    stop_price = stop_utils.valid_stop_for_fill(
        direction, fill_price, stop_price,
        entry_price if entry_price != 0.0 else fill_price)
    pmt_signal = {
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "flatten_first": flatten_first,
    }
    _executor.place_entry(pmt_signal, None)
    if not getattr(_executor, "_entry_is_live", True):
        # Entry was blocked by window gate — don't update position state or log
        return
    pos = _load_pos()
    pos["active"] = {
        "direction": direction,
        "fill_price": fill_price,
        "stop": stop_price,
        "cautious": "no",
        "contracts": 2,
        "time": now,
        "source": source,
    }
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos.pop("stop_entry_source", None)
    # SMT-v2 Phase 1: freeze the management direction + cautious ladder into active —
    # immutable for the life of the trade. This path does NOT recompute cautious (manual
    # entries are discretionary), so the frozen ladder copies whatever the live hypothesis
    # currently holds at entry — acceptable and consistent with today's manual behavior.
    hyp = smt_state.load_hypothesis()
    smt_state.freeze_active_mgmt(pos["active"], direction, hyp)
    _save_pos(pos)
    # Re-anchor the cautious ladder to the actual fill — a manual market entry (or any
    # market entry) otherwise leaves hypothesis.json's cautious levels at the stale
    # formation anchor, so the cautious break can trail too tight and stop the position
    # out prematurely (2026-06-12: a manual re-entry was stopped out on the old ladder).
    # Mirrors the STP->MKT downgrade fill path; no-op while the manual direction lock is
    # set. Best-effort (never blocks the recorded entry).
    _recompute_cautious_at_fill(fill_price)
    _log({"kind": "market-entry", "time": now, "direction": direction,
          "entry_price": entry_price, "stop_price": stop_price})
    # Broker reconciliation (GIL-36): a market fill must rest a valid SL — verify it at the
    # broker and correct a rejected leg / rejected entry. intended_entry is the original
    # trigger when given, else the assumed fill (manual entries pass 0.0).
    _spawn_reconcile(direction, entry_price if entry_price != 0.0 else fill_price,
                     stop_price, fill_price)


def move_stop_entry(new_entry_price: float, new_stop_price: float, direction: str, *, force: bool = False) -> None:
    """Cancel existing unfilled stop entry and replace. Reads old entry_price from position.json.

    Gated on no-open-position: the executor cancels the resting STP via a PMT ``close``,
    which is blanket (it also flattens any open position). To avoid flattening a real
    trade, skip when position.json shows an active position unless ``force=True``.
    """
    now = _now_et()
    pos = _load_pos()
    if not force and pos.get("active"):
        print("[live_orders] move_stop_entry: active position present — NOT moving the stop entry "
              "(PMT 'close' would flatten the open position). Pass force=True to override.", flush=True)
        return
    old_entry = float(pos["stop_entry"]) if pos.get("stop_entry") else new_entry_price
    old_pmt = {
        "direction": direction,
        "entry_price": old_entry,
        "stop_price": new_stop_price,
        "stop_fill_bars": 1,
    }
    new_pmt = {
        "direction": direction,
        "entry_price": new_entry_price,
        "stop_price": new_stop_price,
        "stop_fill_bars": 1,
        "current_price": _current_price(),
    }
    # Cross-process truth: if position.json shows a placed (non-`unplaced`) stop entry, the
    # old STP order IS working at the broker and must be cancelled even from a separate
    # `trade.py move` CLI process (where executor._entry_is_live is False). Without this the
    # move places the new order but leaves the old one resting → duplicate (D6, 2026-06-11).
    _old_placed = bool(pos.get("stop_entry")) and not pos.get("stop_entry_unplaced")
    _executor.modify_stop_entry(old_pmt, new_pmt, None, placed_at_broker=_old_placed)
    pos["stop_entry"] = str(new_entry_price)
    pos["pending_stop"] = new_stop_price
    if getattr(_executor, "_entry_is_live", True):
        pos.pop("stop_entry_unplaced", None)
    _save_pos(pos)
    _log({"kind": "move-stop-entry", "time": now, "direction": direction,
          "new_entry_price": new_entry_price, "new_stop_price": new_stop_price,
          "old_entry_price": old_entry})


def stop_entry_filled(direction: str, stop_price: float, fill_price: float = 0.0) -> None:
    """Stop entry just filled — log and update active.stop in position.json.

    The real S/L was already embedded in the STP order at placement time, so
    no separate update_stop_loss call is needed here.

    SMT-v2 Phase 1: no freeze here — this path only updates active["stop"] on an
    already-active position; it never creates active, so the frozen management
    fields were already written at the originating fill (strategy fill / downgrade).
    """
    now = _now_et()
    pos = _load_pos()
    _entry_anchor = 0.0
    if pos.get("active"):
        pos["active"]["stop"] = stop_price
        _save_pos(pos)
        try:
            _entry_anchor = float(pos.get("stop_entry") or 0.0)
        except (TypeError, ValueError):
            _entry_anchor = 0.0
    else:
        print("[live_orders] stop_entry_filled: active position absent — position.json not updated", flush=True)
    _log({"kind": "stop-entry-filled", "time": now, "direction": direction,
          "price": fill_price, "stop_price": stop_price})
    # Broker reconciliation (GIL-36): a normal resting STP just confirmed filled — verify the
    # SL leg rests at the broker. intended_entry = the resting STP trigger (the stop_entry
    # anchor from position.json) when known, else the reported fill; intended_stop = the
    # embedded SL; fill = the reported fill price.
    _fill_anchor = float(fill_price) if fill_price else _entry_anchor
    _spawn_reconcile(direction, _entry_anchor if _entry_anchor else _fill_anchor,
                     stop_price, _fill_anchor)


def cancel_stop_entry(reason: str = "user-requested", force: bool = False) -> None:
    """Cancel pending stop entry. No-op if stop_entry is empty (unless force=True). Logs, dispatches close, clears position.json.

    Gated on no-open-position: PMT ``close`` (used to cancel the resting STP) is blanket
    and would also flatten any open position. Skip when position.json shows an active
    position unless ``force=True`` (force WILL flatten the open position).
    """
    pos = _load_pos()
    if not force and not pos.get("stop_entry"):
        return
    if not force and pos.get("active"):
        print("[live_orders] cancel_stop_entry: active position present — NOT sending 'close' to "
              "cancel the stop entry (PMT 'close' would flatten the open position). Pass "
              "force=True to cancel anyway (this WILL flatten the open position).", flush=True)
        return
    now = _now_et()
    entry_price = float(pos["stop_entry"]) if pos.get("stop_entry") else 0.0
    # Send the broker cancel whenever the entry was actually placed at the broker. Gate on the
    # PERSISTED stop_entry_unplaced flag (cross-process truth), NOT the executor's per-process
    # _entry_is_live — that flag is only True in the process that sent the entry (the
    # orchestrator), so a `trade.py cancel` from a separate CLI process saw it False and never
    # cancelled, leaving a working STP order at the broker while position.json showed it gone
    # (incident 2026-06-04 09:05). Mirrors the stop-entry-cancelled dispatch path.
    if not pos.get("stop_entry_unplaced"):
        _executor.place_close("cancel-stop")
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["conf_bar_entry"] = {}
    pos.pop("stop_entry_unplaced", None)
    pos.pop("stop_entry_source", None)
    _save_pos(pos)
    _log({"kind": "cancel-stop-entry", "time": now, "entry_price": entry_price, "reason": reason})


def close_position(price: float, reason: str = "user-requested") -> None:
    """Market-close active position. Logs, dispatches close, clears active in position.json."""
    now = _now_et()
    _executor.place_close("close")
    pos = _load_pos()
    pos["active"] = {}
    pos["stop_entry"] = ""
    pos["stop_direction"] = ""
    pos["conf_bar_entry"] = {}
    # Also clear the cautious exit reference bar — a closed position must leave no stale
    # conf_bar_exit behind (mirrors the strategy/trend exit paths, e.g. trend.py
    # _clear_position_and_hypothesis). Without this, a manual `trade.py close` leaves the
    # last cautious reference bar in position.json.
    pos["conf_bar_exit"] = {}
    _save_pos(pos)
    # Fall back to current bar mid when caller passes 0.0 (e.g. trade.py close).
    resolved_price = float(price) if float(price) != 0.0 else _current_price()
    _log({"kind": "market-close", "time": now, "price": resolved_price, "reason": reason})


def update_stop_loss(stop_price: float, reason: str = "user-requested", direction: str | None = None) -> None:
    """Update protective stop on active position. Logs, dispatches update_sl, updates active.stop.

    direction: override the direction read from position.json (used with --force when active is empty).
    """
    now = _now_et()
    pos = _load_pos()
    resolved = direction if direction is not None else pos.get("active", {}).get("direction", "")
    _executor.update_stop_loss({"direction": resolved, "stop_price": stop_price}, None)
    if pos.get("active"):
        pos["active"]["stop"] = stop_price
        _save_pos(pos)
    _log({"kind": "update-stop-loss", "time": now, "reason": reason, "stop_price": stop_price})


# ---------------------------------------------------------------------------
# Pipeline dispatch — single entry point for all automatic signals
# ---------------------------------------------------------------------------

def dispatch(sig: dict) -> None:
    """Route a pipeline signal to log + executor. Called by SmtV2Dispatcher._emit().

    Direction mapping: pipeline uses "up"/"down", live_orders uses "long"/"short".
    For manual triggers (trade.py), call the specific functions directly instead.
    """
    global _entry_sent_bar_time, _fill_bar_time, _cancel_bar_time, _pending_close_after

    kind = sig.get("kind")
    direction_v2 = sig.get("direction", "none")
    direction = "long" if direction_v2 == "up" else ("short" if direction_v2 == "down" else None)

    # Manual pause: suppress new automatic entries while keeping all exits/management active.
    # Only the three entry kinds are blocked; fills, exits, stop moves and cancels pass through.
    if kind in ("new-stop-entry", "move-stop-entry", "market-entry") and is_paused():
        return

    if kind == "new-stop-entry":
        stop = sig.get("stop")
        if direction and stop is not None:
            place_stop_entry(direction, float(sig["price"]), float(stop))
        if sig.get("time"):
            try:
                _entry_sent_bar_time = pd.Timestamp(sig["time"])
            except Exception:
                _entry_sent_bar_time = None
        return

    if kind == "move-stop-entry":
        stop = sig.get("stop")
        if direction and stop is not None:
            move_stop_entry(float(sig["price"]), float(stop), direction)
        return

    if kind == "stop-entry-filled":
        stop = sig.get("stop")
        if direction and stop is not None:
            stop_entry_filled(direction, float(stop), float(sig.get("price", 0.0)))
        if sig.get("time"):
            try:
                _fill_bar_time = pd.Timestamp(sig["time"])
            except Exception:
                _fill_bar_time = None
        _pending_close_after = None  # Cancel deferred close — position confirmed filled
        return

    if kind == "market-entry":
        # GIL-42 belt-and-suspenders: if the close that immediately preceded this entry
        # ADOPTED a live broker position (sentinel set by recon_on_close.apply_correction),
        # SKIP the market entry — do NOT stack onto the adopted lot. The same-bar direction
        # flip (market-close -> market-entry with flatten_first) inherits this because the
        # flip's close ran _reconcile_on_close synchronously between the close and the paired
        # entry within the same dispatch/bar sequence. Clear the sentinel after honoring it so
        # a later legitimate entry is not blocked.
        if _LIVE:
            _pos = _load_pos()
            if _pos.get("recon_suppress_force_entry"):
                _pos.pop("recon_suppress_force_entry", None)
                _save_pos(_pos)
                _log(sig)
                return
        stop = sig.get("stop")
        if direction and stop is not None:
            place_market_entry(direction, float(sig["price"]), float(stop),
                               flatten_first=bool(sig.get("flatten_first")))
        return

    if kind == "market-close":
        if _pending_close_after is not None and sig.get("time"):
            try:
                _close_ts = pd.Timestamp(sig["time"])
                if _close_ts < _pending_close_after:
                    _log(sig)   # log the deferred signal
                    return      # don't close yet — wait until pending_close_after
                # Time has passed — clear the pending flag and proceed with close
            except Exception:
                pass
        _pending_close_after = None  # clear regardless after processing
        # GIL-42: reconcile the broker BEFORE the close. If the close was a phantom
        # (broker still holds / already flat), adopt or flat the state without sending an
        # order and suppress the close-MKT entirely.
        if _reconcile_on_close(sig):
            _log(sig)
            return
        close_position(float(sig.get("price", 0.0)), sig.get("reason", "strategy"))
        return

    if kind == "stop-exit":
        # Cautious stop-exit: IB stop already fired in normal flow; this is a safety-net
        # market-close in case the stop order didn't execute (e.g. connectivity gap).
        # trend.py has already cleared position+hypothesis in position.json before emitting.
        if _fill_bar_time is not None and sig.get("time"):
            try:
                _exit_ts = pd.Timestamp(sig["time"])
                if _exit_ts - _fill_bar_time < pd.Timedelta(seconds=3):
                    _log(sig)  # still log
                    return     # but don't send the close (too soon after fill)
            except Exception:
                pass
        # GIL-42: reconcile the broker BEFORE the safety-net close.
        if _reconcile_on_close(sig):
            _log(sig)
            return
        _executor.place_close("close")
        _log(sig)
        return

    if kind == "new-stop-exit":
        cbp = sig.get("cautious_break_price")
        if cbp is None:
            cbp = _load_pos().get("active", {}).get("cautious_break_price")
        if cbp is not None:
            _lname = sig.get("level_name", "")
            _reason = f"new-stop-exit:{sig.get('level', '')}" + (f":{_lname}" if _lname else "")
            if sig.get("level") == "secondary":
                # Secondary exit is managed by 1m bar-close check in trend.py.
                # Move stop 1000 pts away from money so wicks never trigger a fill.
                _dir = sig.get("direction", _load_pos().get("active", {}).get("direction", ""))
                _current = float(sig.get("price", 0.0))
                if _current > 0:
                    _far = (_current - 1000.0) if _dir in ("up", "long") else (_current + 1000.0)
                else:
                    _far = 0.0 if _dir in ("up", "long") else 50000.0
                update_stop_loss(_far, reason=_reason)
            else:
                update_stop_loss(float(cbp), reason=_reason)
        _log(sig)
        return

    if kind == "move-stop-exit":
        cbp = sig.get("cautious_break_price")
        if cbp is not None:
            _lname = sig.get("level_name", "")
            _reason = f"move-stop-exit:{sig.get('level', '')}" + (f":{_lname}" if _lname else "")
            if sig.get("level") == "secondary":
                # IB stop already at 0/50000 for secondary — no update needed.
                pass
            else:
                update_stop_loss(float(cbp), reason=_reason)
        _log(sig)
        return

    if kind == "cancel-stop-entry":
        # Emitted by strategy.py on direction change or window close. Falls through to
        # log-only normally, but must also clear the unplaced flag when set.
        _pos = _load_pos()
        if _pos.get("stop_entry_unplaced"):
            _pos.pop("stop_entry_unplaced", None)
            _save_pos(_pos)
        _log(sig)
        return

    if kind == "stop-entry-cancelled":
        # Emitted by session_pipeline when a market-entry overwrites a pending stop.
        # position.json already cleared by the pipeline before emitting this signal.
        _pos = _load_pos()
        if _pos.get("stop_entry_unplaced"):
            # Entry was never placed at broker — log only, no cancel needed.
            _pos.pop("stop_entry_unplaced", None)
            _save_pos(_pos)
            _log(sig)
            return
        # Normal path: entry was live at broker, cancel it.
        if _entry_sent_bar_time is not None and sig.get("time"):
            try:
                _cancel_ts = pd.Timestamp(sig["time"])
                if _cancel_ts - _entry_sent_bar_time < pd.Timedelta(seconds=1):
                    _log(sig)  # still log the signal
                    return     # but don't dispatch the cancel (too soon after entry)
            except Exception:
                pass
        _executor.place_close("cancel-stop")
        _log(sig)
        if sig.get("time"):
            try:
                _cancel_bar_time = pd.Timestamp(sig["time"])
                _pending_close_after = _cancel_bar_time + pd.Timedelta(seconds=3)
            except Exception:
                _cancel_bar_time = None
                _pending_close_after = None
        return

    if kind == "stopped-out":
        # IB's protective stop already executed — clear position state and log.
        _fill_bar_time = None
        # Capture the cautious flag BEFORE the reconcile (which may clear active): a real
        # cautious stop must still reset the hypothesis even when the reconcile takes over
        # the state clear (see below).
        _pre_cautious = _load_pos().get("active", {}).get("cautious", "no") not in ("no", "")
        # GIL-42: reconcile the broker BEFORE clearing state. ADOPT restores active (the
        # stop was a phantom and the broker still holds the lot) -> the hypothesis must NOT
        # be reset (the position is real). SUPPRESS_CLOSE confirms a real stop (broker flat)
        # and leaves active empty -> we must still run the cautious-stop hypothesis reset
        # the normal clear below would have done, so the strategy doesn't re-enter on a stale
        # direction after a real cautious stop (GIL-8 manual-lock release preserved).
        if _reconcile_on_close(sig):
            if not _load_pos().get("active"):
                # Reconcile flattened (real stop confirmed) — preserve the cautious reset.
                if _pre_cautious:
                    from smt_state import load_hypothesis, save_hypothesis
                    hyp = load_hypothesis()
                    hyp["direction"] = "none"
                    hyp["manual"] = False  # GIL-8: direction cleared -> release manual lock
                    save_hypothesis(hyp)
            _log(sig)
            return
        pos = _load_pos()
        if pos.get("active", {}).get("cautious", "no") not in ("no", ""):
            # Cautious stop fired: also reset hypothesis so the strategy doesn't re-enter
            # on a stale direction. (Non-cautious stops keep the hypothesis alive for re-entry.)
            from smt_state import load_hypothesis, save_hypothesis
            hyp = load_hypothesis()
            hyp["direction"] = "none"
            hyp["manual"] = False  # GIL-8: direction cleared → release the manual lock
            save_hypothesis(hyp)
        pos["active"] = {}
        pos["stop_entry"] = ""
        pos["stop_direction"] = ""
        pos["conf_bar_entry"] = {}
        _save_pos(pos)
        _log(sig)
        return

    # All other signal kinds (new-hypothesis, trend-broken, level-swept,
    # smt-div, ath-crossed, dynamic-ath-crossed, …): log only.
    _log(sig)


# ---------------------------------------------------------------------------
# Manual commands
# ---------------------------------------------------------------------------

def trend_broken() -> dict:
    """Reset hypothesis direction to 'none', cancel any pending stop entry, log trend-broken.

    Calls cancel_stop_entry BEFORE clearing position state so the broker cancel
    actually fires (cancel_stop_entry is a no-op if stop_entry is already empty).
    The next 5-minute bar will form a fresh hypothesis via the normal pipeline.

    Usage:
        import live_orders; live_orders.trend_broken()
        python trade.py trend-broken
    """
    from smt_state import load_hypothesis, save_hypothesis

    smt_state.ensure_live_state_dir()  # standalone caller: resolve the session state folder
    hypothesis = load_hypothesis()
    broken_dir = hypothesis.get("direction", "none")

    # Cancel broker order first, while stop_entry is still set in position.json.
    cancel_stop_entry(reason="trend-broken-manual")

    hypothesis["direction"] = "none"
    hypothesis["manual"] = False  # GIL-8: trend-broken is a release path for the manual lock
    save_hypothesis(hypothesis)

    # Clear conf_bar_entry in case cancel_stop_entry didn't (no pending stop).
    pos = _load_pos()
    pos["conf_bar_entry"] = {}
    _save_pos(pos)

    event = {
        "kind":             "trend-broken",
        "time":             _now_et(),
        "direction":        "none",
        "broken_direction": broken_dir,
        "source":           "manual",
    }
    _log(event)
    print(
        f"[live_orders] trend-broken fired (was {broken_dir!r}) — "
        "next 5m bar will form a fresh hypothesis.",
        flush=True,
    )
    return event


def hypothesis() -> list:
    """Force a fresh hypothesis evaluation regardless of current direction.

    Resets hypothesis direction to 'none', loads the latest bar parquets from disk,
    calls run_hypothesis, and logs all resulting signals to events.jsonl.

    Usage:
        import live_orders; live_orders.hypothesis()
        python trade.py hypothesis
    """
    import pandas as pd
    import hypothesis as _hyp_mod
    from smt_state import load_hypothesis, save_hypothesis

    now      = datetime.datetime.now(_ET)
    now_ts   = pd.Timestamp(now)
    today    = now.date()
    now_str  = now.isoformat()

    smt_state.ensure_live_state_dir()  # standalone caller: resolve the session state folder
    hyp     = load_hypothesis()
    old_dir = hyp.get("direction", "none")
    hyp["direction"] = "none"
    hyp["manual"] = False  # GIL-8: explicit re-evaluation releases the manual lock
    save_hypothesis(hyp)

    mnq_path = paths.general_live_dir() / "MNQ_1m.parquet"
    mes_path = paths.general_live_dir() / "MES_1m.parquet"
    if not mnq_path.exists() or not mes_path.exists():
        print("[live_orders] hypothesis: bar parquets not found — is the orchestrator running?", flush=True)
        return []

    hist_mnq_1m = pd.read_parquet(mnq_path)
    hist_mes_1m = pd.read_parquet(mes_path)
    today_mnq   = hist_mnq_1m[hist_mnq_1m.index.date == today]
    today_mes   = hist_mes_1m[hist_mes_1m.index.date == today]

    _agg     = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    _14d_ago = now_ts - pd.Timedelta(days=14)
    hist_1hr = (
        hist_mnq_1m[hist_mnq_1m.index >= _14d_ago]
        .resample("1h", label="left").agg(_agg).dropna(subset=["Open"])
    )
    hist_4hr = (
        hist_mnq_1m
        .resample("4h", label="left").agg(_agg).dropna(subset=["Open"])
    )

    signals = _hyp_mod.run_hypothesis(
        now, today_mnq, today_mes,
        hist_mnq_1m, hist_mes_1m,
        hist_1hr=hist_1hr, hist_4hr=hist_4hr,
        skip_position_reset=True,
    )

    if signals:
        for sig in signals:
            _log(dict(sig, source="manual", time=now_str))
            print(
                f"[live_orders] hypothesis {sig.get('kind')} "
                f"direction={sig.get('direction', '?')} "
                f"cautious_initial={sig.get('cautious_price_initial', '?')}",
                flush=True,
            )
    else:
        print(f"[live_orders] hypothesis: no hypothesis formed (was {old_dir!r})", flush=True)

    return signals or []


def set_direction(direction_v2: str) -> bool:
    """Manually flip the hypothesis to a chosen direction and LOCK it (GIL-8).

    One-shot discretionary override: rewrites hypothesis.json for the forced
    direction with a freshly computed cautious ladder (via
    _force_hypothesis_for_direction → build_hypothesis_from_direction; the direction
    is NOT re-derived by the rules), then sets the `manual` lock flag so the
    automatic reset paths (level sweep, session-ATH cross, daily/weekly mid-cross
    and global-trend trend-broken) leave the hypothesis alone until released via
    `trade.py unlock` or `trade.py trend-broken`. The locked cautious ladder is also
    preserved across fills (recompute_cautious_for_fill no-ops while locked).

    If the hypothesis already points the requested way, it is locked as-is (no
    cautious recompute). Returns True when set and locked; False when the rewrite
    didn't take (active position present, or no current price available).

    Usage:
        import live_orders; live_orders.set_direction("down")
        python trade.py set-direction down    (alias: trade.py flip down)
    """
    from smt_state import load_hypothesis, save_hypothesis

    smt_state.ensure_live_state_dir()
    if direction_v2 not in ("up", "down"):
        print(f"[live_orders] set_direction: invalid direction {direction_v2!r} (use up|down)", flush=True)
        return False
    _force_hypothesis_for_direction(direction_v2)
    hyp = load_hypothesis()
    if hyp.get("direction") != direction_v2:
        print("[live_orders] set_direction: hypothesis rewrite did not take (active "
              "position present, or no current price available) — NOT locked", flush=True)
        return False
    hyp["manual"] = True
    save_hypothesis(hyp)
    _log({"kind": "manual-direction-lock", "time": _now_et(), "direction": direction_v2,
          "cautious_price_initial": hyp.get("cautious_price_initial", ""),
          "cautious_price_secondary": hyp.get("cautious_price_secondary", ""),
          "source": "manual"})
    print(f"[live_orders] direction locked {direction_v2!r} — cautious "
          f"{hyp.get('cautious_price_initial', '?')}/{hyp.get('cautious_price_secondary', '?')}; "
          "automatic hypothesis resets suspended until 'trade.py unlock' or trend-broken.",
          flush=True)
    return True


def unlock_direction() -> bool:
    """Release the manual direction lock set by set_direction (GIL-8).

    The direction itself is kept — the strategy's normal reset paths simply apply
    again from here on. Returns False (no-op) when no lock is set.

    Usage:
        import live_orders; live_orders.unlock_direction()
        python trade.py unlock
    """
    from smt_state import load_hypothesis, save_hypothesis

    smt_state.ensure_live_state_dir()
    hyp = load_hypothesis()
    if not hyp.get("manual"):
        print("[live_orders] unlock: no manual direction lock set", flush=True)
        return False
    hyp["manual"] = False
    save_hypothesis(hyp)
    _log({"kind": "manual-direction-unlock", "time": _now_et(),
          "direction": hyp.get("direction", "none"), "source": "manual"})
    print(f"[live_orders] manual direction lock released — direction stays "
          f"{hyp.get('direction', 'none')!r}; automatic resets apply again.", flush=True)
    return True


def _force_hypothesis_for_direction(forced_v2: str) -> None:
    """Rewrite hypothesis.json for a manually forced direction.

    Called before place_stop_entry / place_market_entry when forced_v2 differs
    from the current hypothesis direction, and by set_direction (GIL-8). Cancels any
    pending opposite-direction stop entry, then writes a fresh hypothesis with correct
    cautious prices and mid-cross guard labels so run_trend() doesn't immediately
    fire trend-broken. NOTE: the rewrite builds a fresh hypothesis dict, so it drops
    any manual direction lock — a manual entry against the lock implicitly releases it.

    entry_ranges is left empty — the entry is manual, O5 doesn't apply.
    """
    import hypothesis as _hyp_mod
    from smt_state import load_daily, load_global, load_hypothesis

    smt_state.ensure_live_state_dir()  # standalone caller: resolve the session state folder
    hyp           = load_hypothesis()
    old_direction = hyp.get("direction", "none")

    if forced_v2 == old_direction:
        return  # already aligned, nothing to do

    # Skip rewrite if a position is already active — rewriting hypothesis while holding
    # a trade in the opposite direction would cause run_trend() to fire trend-broken
    # on the next tick, creating a persistent inconsistency.
    if get_position().get("active"):
        print("[live_orders] _force_hypothesis_for_direction: active position present, skipping hypothesis rewrite", flush=True)
        return

    # Cancel any pending stop for the old direction before overwriting hypothesis.
    cancel_stop_entry(reason="direction-override")

    now          = datetime.datetime.now(_ET)
    global_state = load_global()
    daily        = load_daily()
    liquidities  = daily.get("liquidities", [])

    # current_close from bar_state midpoint (same as _current_price()).
    current_close = _current_price()

    if current_close == 0.0:
        print("[live_orders] _force_hypothesis_for_direction: bar_state.json unavailable, skipping hypothesis rewrite", flush=True)
        return

    # Compute mid labels from daily.json liquidities (no live H/L refresh — what
    # matters is the label relative to current price, not the intraday extremes).
    # week_high/low and day_high/low are always kind="level"; FVGs use different names.
    _liq_map = {l["name"]: l["price"] for l in liquidities if l.get("kind") == "level"}
    wh, wl = _liq_map.get("week_high"), _liq_map.get("week_low")
    dh, dl = _liq_map.get("day_high"),  _liq_map.get("day_low")
    weekly_mid = _hyp_mod.compute_mid_label(current_close, wh, wl) if wh and wl else ""
    daily_mid  = _hyp_mod.compute_mid_label(current_close, dh, dl) if dh and dl else ""

    signals = _hyp_mod.build_hypothesis_from_direction(
        forced_v2,
        now,
        current_close,
        liquidities,
        global_state,
        old_direction,
        weekly_mid,
        daily_mid,
        last_liquidity    = hyp.get("last_liquidity", ""),
        divs              = hyp.get("divs", []),
        direction_reason  = {"rule": "forced_manual"},
        # hist_mnq_1m not passed → entry_ranges = []
        skip_veto         = True,
        skip_position_reset = True,
    )

    now_str = now.isoformat()
    for sig in signals:
        _log(dict(sig, source="manual", time=now_str))
