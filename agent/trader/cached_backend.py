"""A thesis backend that serves recordings and only calls the model on a miss.

Contract: this IS a thesis-producing callable, so the Analyzer needs no knowledge of
caching. It returns `(thesis, meta)` -- the widened cycle-2 contract from Task 1.

`allow_calls=False` turns a miss into `NetworkCallRefused` instead of a model call. That
is what makes "a warm-cache replay makes ZERO network calls" a hard, provable gate rather
than an assumption.
"""
from __future__ import annotations

from agent.trader.thesis_cache import ThesisCache, thesis_key


class NetworkCallRefused(RuntimeError):
    """Raised on a cache miss while calls are disallowed."""


def _split(result):
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], (result[1] or {})
    return result, {}


class CachedThesisBackend:
    def __init__(self, inner=None, *, cache=None, model_id="",
                 system_prompt_fn=None, task_prompt="", schema_fn=None,
                 allow_calls=True, boundary_hint=None) -> None:
        """`boundary_hint` is the fallback boundary for the key and the record.

        The plan reads the boundary out of `facts["boundary"]`. On the REAL path there is
        no such key -- `assemble_facts` returns exactly `DECIDE_THESIS_KEYS`, and
        `boundary` is not one of them -- so every real recording keyed on `""` and stored
        `boundary: null`, losing the one field that says which day a recording belongs to.
        The hint restores it without widening the Analyzer->backend contract. `facts`
        still wins when it does carry a boundary.
        """
        self._inner = inner
        self._boundary_hint = boundary_hint
        self._cache = cache if cache is not None else ThesisCache()
        self._model_id = model_id
        self._system_prompt_fn = system_prompt_fn or (lambda: "")
        self._task_prompt = task_prompt
        self._schema_fn = schema_fn or (lambda facts: {})
        self._allow_calls = bool(allow_calls)
        self.hits = 0
        self.misses = 0
        self.calls = 0
        # Refusals are COUNTED as well as raised. `Analyzer._call` catches every
        # exception by design (fail dark, never stall the bar loop), so a raise alone
        # is invisible to the caller: a refused replay would look exactly like a dark
        # day. The counter is what lets `run_replay` turn "this run reached for the
        # model" into a hard failure after the fact.
        self.refusals = 0
        self.last_key = None

    def _boundary(self, facts):
        if isinstance(facts, dict) and facts.get("boundary"):
            return facts.get("boundary")
        return self._boundary_hint or ""

    def _key(self, facts_text, context_text, facts) -> str:
        return thesis_key(
            boundary=self._boundary(facts),
            facts_text=facts_text,
            context_text=context_text,
            system_prompt=self._system_prompt_fn(),
            task_prompt=self._task_prompt,
            schema=self._schema_fn(facts),
            model_id=self._model_id,
        )

    def __call__(self, facts_text, context_text, facts, *, evidence_magnitude=None):
        key = self._key(facts_text, context_text, facts)
        self.last_key = key

        record = self._cache.get_record(key)
        if record is not None:
            self.hits += 1
            # Replay the RECORDED provenance (latency, tokens, retries, verdict) rather
            # than an empty meta: a served recording's provenance is the provenance of
            # the call that produced it, and without this a warm replay reports
            # `call_meta = {}` while claiming to record it on both paths.
            meta = dict(record.get("meta") or {})
            meta.update({"cached": True, "key": key})
            return record["thesis"], meta

        self.misses += 1
        if not self._allow_calls:
            self.refusals += 1
            raise NetworkCallRefused(
                f"thesis cache miss for key {key} while calls are disallowed -- "
                "seed the cache with a real-call run first")
        if self._inner is None:
            self.refusals += 1
            raise NetworkCallRefused(f"cache miss for key {key} and no inner backend")

        self.calls += 1
        thesis, meta = _split(self._inner(facts_text, context_text, facts,
                                          evidence_magnitude=evidence_magnitude))
        # A failed call is NOT recorded: caching it would freeze the date into a
        # permanent dark day that no re-run could recover from.
        #
        # `thesis is None` is only half of it. `decide_thesis` NEVER returns None on
        # failure -- when validation fails past MAX_RETRIES it substitutes a synthetic
        # NEUTRAL block and stamps `verdict="failsafe"`. That is a dict, so the plan's
        # None-guard never fires on the real path and the actual failure mode was the
        # one getting cached (observed on the 2026-08-12 seeding run: retries=2,
        # verdict=failsafe, bias=NEUTRAL, recorded permanently). A failsafe is a call
        # failure, not a judgment, so it is refused the same way. A `ledger_fallback`
        # (ACT_THESIS_FAILSAFE off) is the same call failure given a tradeable thesis;
        # it is not recorded either, so a later --seed run can still get a model answer.
        failsafe = str(meta.get("verdict") or "").lower() in ("failsafe", "ledger_fallback")
        if isinstance(thesis, dict) and not failsafe:
            self._cache.put(key, thesis, meta=meta, boundary=self._boundary(facts))
        meta = dict(meta)
        meta["cached"] = False
        meta["key"] = key
        return thesis, meta


# -- the shared factory (live AND replay) --------------------------------------------- #
#
# Live and replay must build the recording backend the same way, or a live 09:20 call is
# never recorded and a replay of that date has to be re-seeded with a fresh model call
# (a different thesis). These helpers were the replay's private ones; `automation/main.py`
# now builds the live trader through the same factory, so the cache key (boundary,
# facts_text, prompts, schema, model) and the invalidation rules are identical on both
# paths by construction. `agent/trader/replay.py` re-exports them under its old names so
# the gate tests that monkeypatch `replay._real_backend` keep working.

import os as _os


def backend_name() -> str:
    """The backend that will actually answer, RESOLVED the way `run_agent.make_backend`
    resolves it (run_agent.py:511-520): an explicit `ACT_TRADER_BACKEND` wins, otherwise
    OPENROUTER_API_KEY, otherwise ANTHROPIC_API_KEY.

    It must be the RESOLVED name, not a hard-coded default, because it goes into the
    thesis-cache key. Live leaves the variable unset (see .env.example) and auto-selects;
    a warm replay builds no backend at all and has only the environment to go on. If the
    two disagreed the replay would miss every live recording and silently re-call the
    model — which is exactly what happened when live ran OpenRouter while the recordings
    had been made through Anthropic. Keys, not clients: nothing here constructs a backend.
    """
    explicit = (_os.environ.get("ACT_TRADER_BACKEND") or "").strip()
    if explicit:
        return explicit
    if _os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if _os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return ""


def real_thesis_backend():
    """The model-calling thesis backend, imported lazily so a warm-cache replay never
    touches `run_agent` (and therefore never needs an API key). Same env vars and
    defaults in live and replay."""
    from agent.run_agent import make_backend
    from agent.trader.analyzer import thesis_via_decide_thesis
    # `None`, not `backend_name()`: let make_backend do its own auto-selection so an
    # absent key raises ITS error (the caller turns that into a REFUSED start). The
    # resolved name is recomputed for the cache key by `prompt_parts`.
    return thesis_via_decide_thesis(
        make_backend(_os.environ.get("ACT_TRADER_BACKEND") or None,
                     _os.environ.get("ACT_TRADER_MODEL") or None))


def prompt_parts():
    """(system_prompt_fn, task_prompt, schema_fn, model_id) for the cache key.

    `build_system_prompt` (the concatenated KB) and `_TASK_THESIS` are the two prompt
    halves `decide_thesis` assembles, so hashing them makes a doc edit or a task-prompt
    edit invalidate every recording. `model_id` is `<backend>:<resolved model>`.
    `schema_fn` returns `{}` DELIBERATELY: the real schema is derived from facts that
    are already rendered into `facts_text`, which IS keyed (see replay.py history).
    """
    from agent import run_agent as ra
    backend = backend_name()
    # Direct attribute access, NOT getattr-with-a-default: a renamed task prompt must
    # fail loudly rather than silently weaken every recording's invalidation.
    model = _os.environ.get("ACT_TRADER_MODEL") or ra.DEFAULT_MODELS.get(backend, "")
    return (ra.build_system_prompt, ra._TASK_THESIS,
            (lambda facts: {}), "%s:%s" % (backend, model))


def cached_thesis_backend(date, *, inner=None, allow_calls=True, cache=None):
    """The recording backend for session `date` (the boundary hint). Live passes
    `inner=real_thesis_backend()` and `allow_calls=True` (record on miss, serve on hit);
    a warm replay passes `inner=None, allow_calls=False` (refuse on miss)."""
    sys_fn, task, schema_fn, model_id = prompt_parts()
    return CachedThesisBackend(
        inner, cache=cache if cache is not None else ThesisCache(), model_id=model_id,
        system_prompt_fn=sys_fn, task_prompt=task, schema_fn=schema_fn,
        allow_calls=allow_calls, boundary_hint=str(date),
    )
