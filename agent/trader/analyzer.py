"""The once-daily L1 thesis call: trigger, self-contained facts build, persistence.

**Its own trigger, deliberately.** `session_pipeline`'s 09:20 daily re-run is DEAD on
any normal session: only ONE of its two daily transitions fires per calendar date, and
because the CME session opens at 18:00 the prior evening, 00:00 ET always arrives first
and consumes that once-per-date budget
(`tests/test_session_pipeline.py::test_0920_skipped_if_midnight_already_ran` asserts
exactly that). So the Analyzer cannot piggyback on it and carries its own guard.

**Bar time only.** `now` comes from the bar loop. The orchestrator's wall-clock helper
is for scheduling OUTSIDE the loop; reading a clock in here would make cycle-2 replay
impossible.

**Self-contained.** It assembles its own point-in-time view from the bars it is handed
and never reads any store — not the Executor's, and not one of its own. The Executor's
store is the Executor's; the Analyzer's only output into the rest of the system is the
thesis it persists.

**Fail dark, not degraded.** Any failure leaves no standing thesis, and the arm date is
stamped BEFORE the call so a failure can never re-fire (and re-spend) inside the minute.

**Off the bar loop when threaded.** The call is a synchronous HTTP request to a model
plus two heavy fact builds. The bar loop it is reached from is the same per-second IB
tick callback that drives the legacy engine's ORDER EXECUTION, so a 30-120 s model call
would stall live execution for its whole duration. With `threaded=True` (what the graft
uses) `maybe_run` submits to a daemon thread and returns immediately; the result is
picked up by a later bar through `standing_thesis()`. `threaded=False` (the default,
and what the unit tests use) keeps the call inline and deterministic.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

import pandas as pd

from agent.facts.assemble import assemble_facts
from agent.facts.requirements import ANALYZER_REQUIREMENT
from agent.stretch_override import stretch_override

ARM_HOUR = 9
ARM_MINUTE = 20
THESIS_FILE = "thesis_state.json"

# thesis.md §3a — near-maturity WAIT.
#
# A sweep is unusable in either direction until at least one qualifying HTF bar closes.
# When a day/week-tier item is near its next qualifying close but NOT
# `preconfirm_eligible` (not distance-safe and/or not corroborated), the right move is
# to WAIT for that close rather than call on an immature reading.
#
# `manual-l1-thesis/test_l1_thesis_manual.py` simulates this OFFLINE by retargeting the
# boundary FORWARD — it can, because the parquet already holds the future bars. Live it
# cannot: those bars do not exist yet. So the production shape of the same rule is a
# DEFERRAL — hold the call, re-check each bar, and fire once the close has actually
# happened. Every run in `manual-l1-thesis/rerun_finalfinal/` took the retarget branch
# where it applied, so honouring it here is what keeps production comparable to the
# recorded panel.
#
# The wait is capped: the next qualifying 1h close after 09:20 is 10:00 ET, so the cap
# must exceed 40 minutes to be useful at all. Past the cap the Analyzer fires anyway —
# a late thesis still beats no thesis, and an unbounded wait would silently cost whole
# sessions.
NEAR_MATURITY_WAIT = False
MAX_RETARGET_WAIT = pd.Timedelta(minutes=90)


def arm_time():
    """(hour, minute) of the daily arm, in BAR time.

    `ACT_TRADER_ARM_HHMM=HH:MM` overrides the 09:20 default. This exists for shadow
    runs started outside the 09:20 window -- without it, verifying the chain end to end
    means either waiting for the next open or hand-editing a constant, and a
    hand-edited constant is exactly the kind of thing that gets committed by accident.

    Read at CALL time, never cached, so a test can set it with monkeypatch.setenv.
    """
    raw = str(os.environ.get("ACT_TRADER_ARM_HHMM", "")).strip()
    if raw:
        try:
            hh, mm = raw.split(":")
            hh, mm = int(hh), int(mm)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh, mm
        except Exception:
            pass
    return ARM_HOUR, ARM_MINUTE


def thesis_via_decide_thesis(llm_backend, *, docs_root=None):
    """Adapter: wrap a `run_agent` Backend into the callable the Analyzer expects.

    The Analyzer's `backend` argument is a THESIS-PRODUCING CALLABLE
    `(facts_text, context_text, facts, *, evidence_magnitude) -> thesis dict | None`,
    not a raw LLM client — that is what makes the trigger, the gate and the persistence
    testable without an API key.
    """
    def _call(facts_text, context_text, facts, *, evidence_magnitude=None):
        from agent.run_agent import decide_thesis          # lazy: no import at graft time
        kwargs = {"evidence_magnitude": evidence_magnitude}
        if docs_root is not None:
            kwargs["docs_root"] = docs_root
        outcome = decide_thesis(facts_text, context_text, facts, llm_backend, **kwargs)
        return _thesis_block(outcome), _meta_from_outcome(outcome)
    return _call


def _thesis_block(outcome):
    """Pull the decision block out of a `CallOutcome` (or accept a plain dict)."""
    if outcome is None or isinstance(outcome, dict):
        return outcome
    for attr in ("block", "decision", "thesis"):
        got = getattr(outcome, attr, None)
        if isinstance(got, dict):
            return got
    return None


_META_FIELDS = ("latency_sec", "usage", "cost", "retries", "verdict")

# `run_agent.CallOutcome` does NOT name its fields the way the meta contract does: it
# carries `latency_total` and `usage_total`, and no cost at all. Reading only the
# contract names off a real outcome would silently record nothing but `retries` and
# `verdict`, leaving the whole point of the capture (re-fitting the arrival latency from
# real data) unserved. The contract name wins when present; these are the fallbacks.
_META_ALIASES = {"latency_sec": ("latency_total",), "usage": ("usage_total",)}


def _split_result(result):
    """-> (thesis_dict | None, meta_dict).

    The backend contract is widened, not replaced: a bare dict is still valid and
    yields an empty meta, so every cycle-1 caller and test keeps working.
    """
    meta = {}
    if isinstance(result, tuple) and len(result) == 2:
        result, meta = result[0], (result[1] or {})
    thesis = _thesis_block(result)
    if not isinstance(meta, dict):
        meta = {}
    return thesis, {k: v for k, v in meta.items() if k in _META_FIELDS}


def _meta_from_outcome(outcome):
    """Pull the provenance fields off a `CallOutcome`. Absent attributes are dropped
    rather than defaulted, so a missing field is visible as missing.

    `cost` has no source anywhere in the tree today (no price table exists), so it stays
    absent on the real path rather than being fabricated from a guessed rate."""
    out = {}
    for f in _META_FIELDS:
        got = getattr(outcome, f, None)
        if got is None:
            for alias in _META_ALIASES.get(f, ()):
                got = getattr(outcome, alias, None)
                if got is not None:
                    break
        if got is not None:
            out[f] = got
    return out


def stands(thesis) -> bool:
    """A thesis STANDS only when it is directional and names a DOL.

    Confidence deliberately does NOT gate: the calibration table is out of cycle-1
    scope, and a LOW-confidence directional thesis with a DOL is still a plan.
    """
    if not isinstance(thesis, dict):
        return False
    bias = str(thesis.get("bias") or "").upper()
    if bias in ("", "NEUTRAL"):
        return False
    return thesis.get("dol") is not None


class Analyzer:
    def __init__(self, state_dir, backend, *, requirement=ANALYZER_REQUIREMENT,
                 threaded: bool = False, arrival_latency_sec: float = 0.0) -> None:
        self.state_dir = str(state_dir)
        self._backend = backend
        self._requirement = requirement
        self._threaded = bool(threaded)
        self._arrival = float(arrival_latency_sec or 0.0)
        self._thread = None
        self._lock = threading.Lock()
        self._armed_date = None
        self._armed_at = None
        self._thesis = None
        self._health = None
        self._meta: dict = {}
        self._deferred_until = None
        self._deferred_date = None
        self._load()

    # -- persistence ---------------------------------------------------------- #

    @property
    def path(self) -> str:
        return os.path.join(self.state_dir, THESIS_FILE)

    def _load(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8") as fh:
                blob = json.load(fh)
            self._thesis = blob.get("thesis")
            self._health = blob.get("facts_health")
            self._meta = blob.get("call_meta") or {}
            armed = blob.get("armed_date")
            self._armed_date = pd.Timestamp(armed).date() if armed else None
            armed_at = blob.get("armed_at")
            self._armed_at = pd.Timestamp(armed_at) if armed_at else None
        except Exception:
            self._thesis, self._armed_date, self._health = None, None, None
            self._armed_at, self._meta = None, {}

    def _save(self) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            blob = {
                "armed_date": str(self._armed_date) if self._armed_date else None,
                "armed_at": (self._armed_at.isoformat()
                             if self._armed_at is not None else None),
                "thesis": self._thesis,
                "stands": stands(self._thesis),
                "facts_health": self._health,
                "call_meta": self._meta,
                "requirement": {"name": self._requirement.name,
                                "version": self._requirement.version},
            }
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".thesis_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(blob, fh, default=str)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # -- trigger --------------------------------------------------------------- #

    def maybe_run(self, now: pd.Timestamp, bars: dict):
        """Fire once per session date at the arm minute — or at the end of a
        near-maturity wait (thesis.md §3a; see `NEAR_MATURITY_WAIT`).

        Inline mode returns the raw thesis dict the backend produced (or None if it did
        not fire / the call failed). Threaded mode always returns None — the result
        arrives via `standing_thesis()` on a later bar.
        """
        try:
            if now is None:
                return None
            if self._armed_date == now.date():
                return None

            # A deferral is in flight: fire the moment the awaited close has happened.
            if self._deferred_until is not None:
                if now.date() != self._deferred_date:
                    self._clear_deferral()            # a new session; start over
                elif now < self._deferred_until:
                    return None                       # still waiting
                else:
                    self._clear_deferral()
                    return self._arm(now, bars)

            arm_h, arm_m = arm_time()
            if now.hour != arm_h or now.minute != arm_m:
                return None

            wait_until = self._near_maturity_wait(now, bars)
            if wait_until is not None:
                self._deferred_until = wait_until
                self._deferred_date = now.date()
                return None

            return self._arm(now, bars)
        except Exception:
            with self._lock:
                self._thesis = None
                self._meta = {}                # never leave a previous call's provenance
                self._save()                   # attached to a thesis that no longer exists
            return None

    def _arm(self, now: pd.Timestamp, bars: dict):
        """Stamp the date, then make the call (inline or threaded)."""
        try:
            # Stamp BEFORE the call: a raising backend must not re-fire on the next
            # second and spend a second API call. Under the lock so a still-running
            # worker from a previous arm cannot interleave its own persist.
            with self._lock:
                self._armed_date = now.date()
                self._armed_at = now
                self._thesis = None
                self._meta = {}
                self._save()

            if not self._threaded:
                return self._call(now, bars)

            # Freeze the frames: the bar loop mutates/replaces them under us.
            frozen = {tk: (df.copy() if df is not None and len(df) else df)
                      for tk, df in (bars or {}).items()}
            self._thread = threading.Thread(
                target=self._call, args=(now, frozen), daemon=True,
                name="cycle1-analyzer")
            self._thread.start()
            return None
        except Exception:
            with self._lock:
                self._thesis = None
                self._meta = {}
                self._save()
            return None

    # -- thesis.md §3a --------------------------------------------------------- #

    def _clear_deferral(self) -> None:
        self._deferred_until = None
        self._deferred_date = None

    def deferred_until(self):
        """The instant a near-maturity wait ends, or None. Introspection for tests and
        the shadow runbook — a deferred day looks identical to a dark one otherwise."""
        return self._deferred_until

    def _near_maturity_wait(self, now: pd.Timestamp, bars: dict):
        """-> the timestamp to wait for, or None to call now.

        Mirrors the manual harness: only candidates that are NOT `preconfirm_eligible`
        trigger a wait (an eligible one may be acted on now per the §3a prompt
        guidance), and the earliest such close wins. A build failure means no wait —
        never turn a degraded facts build into a silent all-day hold.
        """
        if not NEAR_MATURITY_WAIT:
            return None
        try:
            _ft, _ct, facts, _mag = assemble_facts(None, bars, now)
            pending = [c for c in (facts.get("near_maturity_candidates") or ())
                       if isinstance(c, dict) and not c.get("preconfirm_eligible")]
            if not pending:
                return None
            resolves = [pd.Timestamp(c["resolves_at"]) for c in pending
                        if c.get("resolves_at")]
            resolves = [r for r in resolves if r > now]
            if not resolves:
                return None
            target = min(resolves)
            if target - now > MAX_RETARGET_WAIT:
                return None                            # too far out — call now instead
            return target
        except Exception:
            return None

    def _call(self, now: pd.Timestamp, bars: dict):
        """The actual work: view assembly, then one L1 thesis call.

        There is deliberately NO FactStore here. An earlier revision drove the full
        lookback over a private store purely to stamp a health snapshot — but
        `assemble_facts` ignores the store by design (the Analyzer is self-contained and
        the store belongs to the Executor), so that snapshot described something with no
        bearing on the decision, at the cost of the whole 17-day batch. Provenance is
        now taken from the view that was ACTUALLY sent, which is the honest thing to
        record.
        """
        try:
            facts_text, context_text, facts, magnitude = assemble_facts(None, bars, now)
            health = _view_provenance(facts, facts_text, now)

            # PLAN 37: the deterministic direction override, BEFORE the model call.
            #
            # Placed here rather than in `derive_facts` (computing the stretch belongs
            # there, DECIDING direction does not) and not in `decide_thesis` (that is the
            # LLM adapter and must not carry strategy). This is the seam that owns "the
            # thesis for this session", so it is the seam that may decide not to ask.
            #
            # FAIL-THROUGH, never fail-forward: `_override_thesis` returns None whenever it
            # cannot produce a COMPLETE thesis -- no stretch, criteria unmet, or no DOL menu
            # on the forced side -- and we then call the model exactly as before. A degraded
            # snapshot must lose the override, never invent a direction from it.
            forced = self._override_thesis(facts, now)
            if forced is not None:
                with self._lock:
                    self._health = health
                    self._thesis = forced
                    # No latency, no usage, no retries, no model: `verdict` says which
                    # path produced this so no artifact can read it as a call.
                    self._meta = {"verdict": "stretch_override"}
                    self._save()
                return self._thesis

            result = self._backend(facts_text, context_text, facts,
                                   evidence_magnitude=magnitude)
            thesis, meta = _split_result(result)
            with self._lock:
                self._health = health
                self._thesis = thesis if isinstance(thesis, dict) else None
                self._meta = meta if self._thesis is not None else {}
                self._save()
            return self._thesis
        except Exception:
            with self._lock:
                self._thesis = None
                self._meta = {}
                self._save()
            return None

    def _override_thesis(self, facts: dict, now) -> "dict | None":
        """A complete deterministic thesis, or None to fall through to the model.

        Total by construction: every failure path returns None. The override is an
        optimisation of last resort -- being wrong about direction is survivable, but
        turning a degraded facts snapshot into a confident forced call is not.
        """
        try:
            verdict = stretch_override((facts or {}).get("session_stretch"))
            if not verdict.get("fires"):
                return None
            direction = verdict["direction"]

            # The DOL is picked from `build_menus`' own nearest-first D1 for the FORCED
            # side. Since plan 16 its VALUE is inert -- not the take-profit (T2 picks that
            # at the fill), not an entry gate, not the death level -- but `analyzer.stands()`
            # still requires one, so an empty menu on the forced side means this override
            # cannot produce a standing thesis and must hand back to the model.
            rows = (((facts or {}).get("menus") or {}).get("dol") or {}).get(direction) or ()
            if not rows or rows[0].get("price") is None:
                return None
            d1 = rows[0]

            return {
                "bias": direction,
                # Deliberately absent rather than fabricated: nothing in the trader path
                # reads either, and leaving them None keeps an override thesis visibly
                # distinct from a model one in every artifact.
                "regime": None,
                "confidence": None,
                "dol": {"level": d1.get("level"), "price": float(d1["price"])},
                # Recorded and never acted on (`Executor._death` records
                # `would_have_falsified` and steps over it), so an empty list costs nothing.
                "falsified_if": [],
                "evidence": [],
                "thesis_source": "stretch_override",
                "override_reason": verdict.get("reason"),
                "session_stretch": verdict.get("stretch"),
            }
        except Exception:
            return None

    def pending(self) -> bool:
        """True while a threaded call is still in flight."""
        t = self._thread
        return t is not None and t.is_alive()

    def standing_thesis(self, now=None):
        """The thesis if it stands AND has arrived, else None.

        `now` is BAR time. With `arrival_latency_sec > 0` the thesis is withheld until
        `armed_at + latency` — reproducing, deterministically, the wall-clock delay that
        live gets for free from running the call on a thread. Callers that pass no `now`
        bypass the gate, which is what every cycle-1 caller does.
        """
        with self._lock:
            thesis = self._thesis if stands(self._thesis) else None
            if thesis is None or now is None or self._arrival <= 0:
                return thesis
            if self._armed_at is None:
                return thesis
            if now < self._armed_at + pd.Timedelta(seconds=self._arrival):
                return None
            return thesis

    def armed_at(self):
        """Bar time of the arm, or None. Provenance for the arrival gate."""
        with self._lock:
            return self._armed_at

    def call_meta(self) -> dict:
        """Latency / token usage / cost / retries for the standing thesis's call.

        Provenance only — never sent to the model. Recorded on BOTH the live and replay
        paths so the arrival-latency constant can be re-fitted from real data instead of
        guessed (recorded 09:20 calls ranged 16.3-103.9 s, p50 ~39 s).
        """
        with self._lock:
            return dict(self._meta)

    def facts_health(self):
        """What the facts layer actually held at decision time. Provenance for cycle-2
        replay; never sent to the model."""
        return self._health


def _view_provenance(facts: dict, facts_text: str, now) -> dict:
    """What the model was ACTUALLY given, in a form a cycle-2 replay can diff.

    Counts, not contents. The point is to make the 2026-07-08 failure mode visible: a
    view that is structurally valid but materially empty (no prior-day / prior-week
    levels because the frames were session-only) reads here as `levels: 0` instead of
    passing silently.
    """
    facts = facts or {}
    levels = facts.get("levels") or {}
    return {
        "boundary": str(now) if now is not None else None,
        "degraded": bool(facts.get("degraded")),
        "facts_text_chars": len(facts_text or ""),
        "levels": len(levels),
        "fvg_zones": len(facts.get("fvg_zones") or ()),
        "smt_candidates": len(facts.get("smt_candidates") or ()),
        "near_maturity_candidates": len(facts.get("near_maturity_candidates") or ()),
        "now_price": facts.get("now_price"),
    }
