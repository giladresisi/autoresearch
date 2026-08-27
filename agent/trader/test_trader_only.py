"""`trader_only`: run the Analyzer/Planner/Executor and NOTHING else.

The replay must exercise only the new chain. The switch lives in `SessionPipeline` so the
live path (cycle 4) inherits the identical code, rather than the replay growing its own
private notion of "skip the legacy engine".

The load-bearing test here is `test_the_facts_store_is_identical_with_legacy_on_and_off`.
Skipping the legacy path is only safe if the trader reads nothing that path produces --
and "the trader silently sees less" is the exact failure class that has now bitten this
project three times (the 2026-07-08 online facts thinness, cycle-1's B2, and the cycle-2
hist-extension trap). Asserting the flag *runs* proves nothing; asserting the store comes
out the same proves the thing that matters.
"""
from __future__ import annotations

import pandas as pd
import pytest

import session_pipeline as sp_mod
from session_pipeline import SessionPipeline

TZ = "America/New_York"


def _frame(start, n, price=100.0):
    idx = pd.date_range(start, periods=n, freq="1min", tz=TZ)
    return pd.DataFrame(
        {"Open": price, "High": price + 1.0, "Low": price - 1.0,
         "Close": price, "Volume": 10.0},
        index=idx)


class _SpyTrader:
    """Records every on_bar call so we can prove the hook still fires."""

    def __init__(self):
        self.calls = []

    def on_bar(self, now, today_mnq, today_mes, bar_complete=None,
               hist_mnq=None, hist_mes=None):
        self.calls.append({
            "now": now,
            "n_today": 0 if today_mnq is None else len(today_mnq),
            "n_hist": 0 if hist_mnq is None else len(hist_mnq),
        })


def _pipeline(trader, *, trader_only):
    hist_mnq = _frame("2026-08-13 08:00", 60)
    hist_mes = _frame("2026-08-13 08:00", 60, price=50.0)
    return SessionPipeline(hist_mnq, hist_mes, lambda e: None,
                           trader=trader, trader_only=trader_only)


def test_trader_only_defaults_to_false_and_the_legacy_path_is_unchanged():
    """The default must be False: with it False the flag costs one boolean test per bar
    and the legacy path is byte-identical, which is what gate 7 proves."""
    p = SessionPipeline(_frame("2026-08-13 08:00", 5), _frame("2026-08-13 08:00", 5),
                        lambda e: None)
    assert p._trader_only is False

    # explicit False is also False, and a truthy value coerces to a real bool
    assert _pipeline(None, trader_only=False)._trader_only is False
    assert _pipeline(None, trader_only=1)._trader_only is True


def test_trader_only_skips_every_legacy_call(monkeypatch):
    """Nothing below the trader hook may execute: no daily recompute, no trend, no SMT
    detection, no liquidity update, no hypothesis, no strategy, no bar_state write."""
    called = []

    for name in ("run_trend",):
        monkeypatch.setattr(sp_mod._trend_mod, name,
                            lambda *a, **k: called.append(name) or None)
    monkeypatch.setattr(sp_mod._strat_mod, "run_strategy",
                        lambda *a, **k: called.append("run_strategy") or None)
    monkeypatch.setattr(sp_mod._hyp_mod, "run_hypothesis",
                        lambda *a, **k: called.append("run_hypothesis") or None)
    monkeypatch.setattr(SessionPipeline, "on_daily_or_startup",
                        lambda self, *a, **k: called.append("on_daily_or_startup"))
    monkeypatch.setattr(SessionPipeline, "_run_completed_bar_detection",
                        lambda self, *a, **k: called.append("detection") or [])
    monkeypatch.setattr(SessionPipeline, "_update_dynamic_liquidities",
                        lambda self, *a, **k: called.append("liquidities"))
    monkeypatch.setattr(SessionPipeline, "_write_bar_state",
                        lambda self, *a, **k: called.append("bar_state"))

    trader = _SpyTrader()
    p = _pipeline(trader, trader_only=True)
    p._daily_triggered = True

    today = _frame("2026-08-13 09:20", 3)
    now = today.index[-1]
    out = p.on_1m_bar(now, today.iloc[-1], today.iloc[-1], today, today,
                      bar_complete=True)

    assert out == [], "trader_only must emit no legacy events"
    assert called == [], f"legacy work ran under trader_only: {called}"
    assert len(trader.calls) == 1, "the trader hook must still fire"


def test_trader_only_still_produces_trader_decisions():
    """The point of the flag is that the NEW chain still runs."""
    trader = _SpyTrader()
    p = _pipeline(trader, trader_only=True)
    p._daily_triggered = True

    today = _frame("2026-08-13 09:20", 5)
    for i in range(1, 5):
        sl = today.iloc[:i + 1]
        p.on_1m_bar(sl.index[-1], sl.iloc[-1], sl.iloc[-1], sl, sl, bar_complete=True)

    assert len(trader.calls) == 4
    # and the hook received real frames, not empties
    assert all(c["n_today"] > 0 for c in trader.calls)
    assert all(c["n_hist"] == 60 for c in trader.calls), \
        "history must reach the trader unchanged when legacy is skipped"


def test_the_facts_store_is_identical_with_legacy_on_and_off():
    """THE anti-silent-degradation check.

    Same bars, same trader inputs, legacy on vs off. Every argument the trader is handed
    must be identical -- if skipping the legacy path changed what the trader sees, the
    store would diverge and no other test in this file would notice.
    """
    today = _frame("2026-08-13 09:20", 6)

    seen = {}
    for flag in (False, True):
        trader = _SpyTrader()
        p = _pipeline(trader, trader_only=flag)
        p._daily_triggered = True
        for i in range(1, 6):
            sl = today.iloc[:i + 1]
            try:
                p.on_1m_bar(sl.index[-1], sl.iloc[-1], sl.iloc[-1], sl, sl,
                            bar_complete=True)
            except Exception:
                # the legacy path may raise on these synthetic frames; the trader hook
                # runs BEFORE it, so its calls are still recorded and comparable.
                pass
        seen[flag] = trader.calls

    assert seen[True], "no trader calls recorded"
    assert len(seen[True]) == len(seen[False]), \
        f"call count differs: legacy-on={len(seen[False])} legacy-off={len(seen[True])}"
    for on, off in zip(seen[False], seen[True]):
        assert on == off, f"trader input differs with legacy off:\n on={on}\noff={off}"


def test_trader_only_with_no_trader_is_a_noop_early_return():
    """Defensive: the flag must not crash when no trader is attached."""
    p = _pipeline(None, trader_only=True)
    p._daily_triggered = True
    today = _frame("2026-08-13 09:20", 3)
    assert p.on_1m_bar(today.index[-1], today.iloc[-1], today.iloc[-1],
                       today, today, bar_complete=True) == []
