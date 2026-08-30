"""Primary-mode runner (spec §3 `primary`) — assembles and drives the v2 loop.

Wraps the whole v2 stack (JSON bus + async DecisionService + TradeDirector + mechanism
adapter) behind two hooks the pipeline calls: `on_session_open(now, frames)` and
`on_bar(now, frames)`. It builds the per-trigger facts snapshot (reusing the Phase-1
`build_snapshot` facts adapter) for AI calls and a deterministic `MarketView` for per-bar
predicate evaluation. The DecisionService runs synchronously in the backtest (`pump()` after
each drive → deterministic; arrival is offset by the latency budget) and threaded when live.

Under the StubBackend the thesis is a NEUTRAL/LOW fail-safe → the director never leaves
THESIS_LOW_CONF, never arms a SETUP, and the mechanism adapter is never invoked → the primary
backtest produces zero trades. That is the intended structure-POC behaviour: this validates
the plumbing / state machine / contracts, not decision quality (deferred with the internals).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_AGENT)
for _p in (_HERE, os.path.join(_AGENT, "contracts"), os.path.join(_AGENT, "decisions"),
           _AGENT, _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from predicates import MarketView, TIMEFRAMES           # noqa: E402
from bus import DecisionBus                              # noqa: E402
from decision_service import DecisionService             # noqa: E402
from mechanism_adapter import RecordingMechanismAdapter  # noqa: E402
from trade_director import TradeDirector, _self_report_confidence  # noqa: E402

_TF_RULE = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}
_CLOSES_TAIL = 128


class PrimaryRunner:
    def __init__(self, run_dir, backend, *, date: str = "", docs_root=None,
                 confidence_fn=None, latency_sec: float = 79.0, threaded: bool = False):
        self.run_dir = Path(run_dir)
        self.backend = backend
        self._docs_root = docs_root
        self.bus = DecisionBus(self.run_dir, date=date)
        self.audit_path = self.run_dir / "ai_decisions_audit.jsonl"
        self.mechanism = RecordingMechanismAdapter()
        self.director = TradeDirector(
            provider=None, mechanism=self.mechanism,
            confidence_fn=confidence_fn or _self_report_confidence)
        self.service = DecisionService(
            run_l1=self._run_l1, run_l2=self._run_l2, bus=self.bus,
            audit_path=self.audit_path, sink=self.director, latency_sec=latency_sec,
            threaded=threaded)
        self.director.provider = self.service
        self._last_min: Optional[pd.Timestamp] = None

    # -- AI call wrappers (bound into the DecisionService) ------------------ #
    def _run_l1(self, fr):
        from run_agent import decide_thesis, DOCS_ROOT
        return decide_thesis(fr["facts_text"], "", fr["facts"], self.backend,
                             docs_root=self._docs_root or DOCS_ROOT)

    def _run_l2(self, fr, thesis):
        from run_agent import decide_plan, DOCS_ROOT
        return decide_plan(fr["facts_text"], "", fr["facts"], thesis, self.backend,
                           docs_root=self._docs_root or DOCS_ROOT)

    # -- facts snapshot + market view (one snapshot per call) --------------- #
    def _snapshot(self, frames):
        """build_snapshot, guaranteed not to raise — a degraded/failed snapshot returns
        None so callers stub-degrade (no thesis / inert MarketView)."""
        from facts_adapter import build_snapshot
        try:
            snap = build_snapshot(frames)
            return None if getattr(snap, "degraded", False) else snap
        except Exception:
            return None

    @staticmethod
    def _facts_ref_from(snap, now, trigger) -> dict:
        if snap is None:
            return {"facts_text": "", "facts": {}, "facts_hash": "<degraded>",
                    "trigger_ts": now, "now": now, "trigger": trigger}
        return {"facts_text": snap.text, "facts": snap.validator_dict,
                "facts_hash": snap.content_hash, "trigger_ts": now, "now": now,
                "trigger": trigger}

    def _market_view_from(self, snap, now, frames) -> MarketView:
        price, swept, depleted = None, set(), set()
        if snap is not None:
            vd = snap.validator_dict or {}
            price = vd.get("now_price")
            for name, lvl in (vd.get("levels") or {}).items():
                if isinstance(lvl, dict):
                    if lvl.get("swept"):
                        swept.add(name)
                    if lvl.get("depleted"):
                        depleted.add(name)
        else:
            price = self._last_close(frames.get("mnq_today"))
        closes = self._closes_by_tf(frames.get("mnq_today"))
        return MarketView(price=price, closes_by_tf=closes, swept=swept, depleted=depleted,
                          now=now, since=self.director.thesis_issued_at)

    @staticmethod
    def _last_close(today):
        if today is None or len(today) == 0:
            return None
        df = today.rename(columns=str.lower)
        return float(df["close"].iloc[-1]) if "close" in df.columns else None

    def _referenced_levels(self) -> set:
        """Level names the STANDING thesis/plan predicates reference — the only ones whose
        sweep/depletion state a per-bar predicate could read. When empty (the stub /
        no-directional case) the per-bar MarketView needs no full facts snapshot."""
        from predicates import referenced_levels
        names: set = set()
        t = self.director.thesis
        if t is not None:
            for fld in (t.falsified_if,):
                for p in fld or []:
                    names |= referenced_levels(p)
            for p in (t.recall or {}).get("events") or []:
                names |= referenced_levels(p)
        p_ = self.director.plan
        if p_ is not None:
            for fld in (p_.setup_falsified_if,):
                for pr in fld or []:
                    names |= referenced_levels(pr)
        return names

    @staticmethod
    def _closes_by_tf(today) -> dict:
        if today is None or len(today) == 0:
            return {}
        df = today.rename(columns=str.lower)
        col = "close" if "close" in df.columns else None
        if col is None:
            return {}
        out = {}
        for tf in TIMEFRAMES:
            try:
                s = df[col].resample(_TF_RULE[tf], label="left").last().dropna()
                out[tf] = [float(x) for x in s.tail(_CLOSES_TAIL).tolist()]
            except Exception:
                out[tf] = []
        return out

    # -- pipeline hooks ----------------------------------------------------- #
    def on_session_open(self, now, frames) -> None:
        # Deliver any matured decisions on THIS (pipeline) thread before/around the open call.
        self.service.deliver_ready(now)
        snap = self._snapshot(frames)
        self.director.on_session_open(now, self._facts_ref_from(snap, now, "session_open"))
        if not self.service.threaded:
            self.service.process()          # make the call; delivery waits until arrival
        self.service.deliver_ready(now)

    def on_bar(self, now, frames) -> None:
        """Drive the director once per new minute (bar close): first apply any decisions that
        have MATURED (trigger + latency reached — modelling call latency identically in
        backtest and live), evaluate predicates, let the director schedule recall/plan calls,
        then process them (sync). Delivery always happens on this thread (never the worker's),
        so director state has a single mutator.

        Cost control (compute_facts is expensive): build the FULL facts snapshot only when
        the standing decisions reference level sweep/depletion state; otherwise a light
        price+closes MarketView. The AI facts_ref is a thunk — built only if a call fires."""
        minute = now.floor("1min")
        if minute == self._last_min:
            return
        self._last_min = minute
        self.service.deliver_ready(now)     # apply matured decisions (both modes)
        snap = self._snapshot(frames) if self._referenced_levels() else None
        mv = self._market_view_from(snap, now, frames)
        facts_ref = lambda: self._facts_ref_from(snap if snap is not None
                                                 else self._snapshot(frames), now, "bar")
        self.director.on_bar(now, mv, facts_ref=facts_ref)
        if not self.service.threaded:
            self.service.process()          # process new requests; they mature on a later bar

    def finalize(self) -> None:
        # Session end: process anything still queued, then deliver everything remaining
        # (ignore the arrival gate — the session is over) so the audit/bus are complete.
        if self.service.threaded:
            self.service.close()
        else:
            self.service.process()
        self.service.deliver_ready(None)
