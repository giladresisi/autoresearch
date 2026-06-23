# Broker reconcile-on-close (GIL-42)

EXECUTION_MODE: lightweight

EXECUTOR DIRECTIVE: Implement this as a contained, sequential, LIVE-ONLY change in this worktree.
Do TDD: write the unit tests in `tests/test_reconcile_on_close.py` first (fake reader, no
browser), then implement. All new behavior must live behind the `live_orders._LIVE`
(`LIVE_TRADING=true`) guard and the live-only `live_orders.dispatch()` seam, which is NEVER
invoked in backtest/regression (backtest pipelines emit to `day_events.append` /
`_emit_v2_signal`, not to `dispatch`). Do NOT touch `strategy.py`, `trend.py`, `hypothesis.py`,
or any per-bar algorithm path. After implementation, run `pytest tests/test_reconcile_on_close.py
tests/test_reconcile.py` and a 1s A/B on one date (e.g. 2026-06-22) confirming BYTE-IDENTICAL
trades vs baseline (the reconcile path must be inert offline). Leave everything UNSTAGED; do not
commit or push.

---

## Problem (verified)

The position model is bar-assumed; there is no broker fill confirmation (PMT_FILLS_URL was
dropped). When the IB bar feed momentarily prints a level Tradovate never traded through, the
strategy records a PHANTOM close, marks itself flat, and re-enters while the broker is still in
the position. New entries STACK; Tradovate FIFO matches a stale stop fill to the wrong (newer)
lot → a far larger realized loss. Verified -$428 on 2026-06-22 evening (sessions/2026-06-23/).

The GIL-36 reconciler that should have caught this was DEAD all session: literal `→` chars in
`broker_recon/reader.py` `_warn()` f-strings crashed on Windows cp1252 stdout. It is also a
headed/placeholder DOM reader (selectors never confirmed) and reconciled only AFTER ENTRY, not
on close.

## Architecture (verified anchors)

- **Single live seam:** `live_orders.dispatch(sig)` (`live_orders.py:681`) is called ONLY by
  `automation/main.py` `SmtV2Dispatcher._emit` (`automation/main.py:1016-1020`). Backtest
  (`backtest_smt.py:1355` → `day_events.append`) and signal (`signal_smt.py:846` →
  `_emit_v2_signal`) never call it. Therefore anything added inside `dispatch` is inherently
  live-only — but we still gate on `_LIVE` defensively.
- **Live guard:** `live_orders._LIVE = os.getenv("LIVE_TRADING","false").lower()=="true"`
  (`live_orders.py:24`). Existing live-only hooks (`_get_recon_reader`, `_spawn_reconcile`)
  already return early when `not _LIVE` (`live_orders.py:99-100, 121-122`).
- **Close kinds routed by dispatch:**
  - `market-close` → `live_orders.py:734-746` (calls `close_position` → `_executor.place_close("close")`).
  - `stop-exit`    → `live_orders.py:748-762` (safety-net `place_close("close")`).
  - `stopped-out`  → `live_orders.py:839-857` (broker stop already executed; clears state, NO order).
- **Immediate-commit entry routed by dispatch:** `market-entry` → `live_orders.py:727-732`
  (`place_market_entry`, optionally `flatten_first`). STP→MKT downgrade is internal to
  `place_stop_entry`/`_register_downgraded_fill` (`live_orders.py:262-293`).
- **Resting-entry kind (no reconcile needed):** `new-stop-entry` → `live_orders.py:698-707`
  (resting STP, harmless).
- **Phantom triggers (pipeline, emit `stopped-out`):** same-bar stop check
  `session_pipeline.py:1537-1556`; trend-driven close paths emit via `self._emit` at
  `session_pipeline.py:1368` (stop-exit / market-close from trend) and the strategy-driven
  `market-close` / `stopped-out` at `session_pipeline.py:1519-1525`. All of these reach
  `dispatch` in live mode.
- **Direction-flip same bar:** `market-close` then paired `market-entry` arises from
  `session_pipeline.py:1519-1523` (sets `_force_entry_eval_after = now.floor("1min")`,
  `_force_market_entry = True`) AND `strat_sig["flatten_first"]=True` at
  `session_pipeline.py:1505-1506`. The flip's flatten is `place_market_entry(..., flatten_first=True)`.
- **Re-entry arming lives on the pipeline, NOT position.json:**
  `SessionPipeline._force_entry_eval_after` (`session_pipeline.py:292`), consumed at
  `session_pipeline.py:1478-1498`. `dispatch` cannot reach it directly → see "suppress re-entry"
  below.
- **Position state shape (`active`)** written at `live_orders.py:323-331, 497-505`:
  `{time, fill_price, direction("long"/"short"), stop, contracts, cautious, source, ...}`
  plus frozen mgmt fields from `smt_state.freeze_active_mgmt` (`smt_state.py:230-257`:
  `mgmt_direction`, `cautious_initial[/_level]`, `cautious_secondary[/_level]`, `backing_tier`).
  `failed_entries` / `cautious_dist_shrinks` are top-level position.json ints (incremented at
  `session_pipeline.py:1545-1546`). `MAX_FAILED_ENTRIES = 2` (`strategy.py:23`).
- **Reconcile primitives to reuse:** `broker_recon.tradovate_login.login_and_select_account`
  (shared headless flow; `reports/get_tradovate_orders.py:90-99` is the headless usage model),
  `broker_recon.reconcile.classify` + side/price helpers, `stop_utils.valid_stop_for_fill`,
  `live_orders.get_position` / `_load_pos` / `_save_pos`, `live_orders._log`,
  `smt_state.freeze_active_mgmt`, `smt_state.load_hypothesis`.

## Live-only guard (concrete)

Every new code path is gated TWICE, both already true only in the orchestrator:
1. It is reachable only from `live_orders.dispatch()` (never called by backtest/signal pipelines).
2. The new helpers early-return when `not live_orders._LIVE`.

No change to `session_pipeline.py` algorithm. The only pipeline touch is OPTIONAL (Step 6,
re-entry suppression) and is itself guarded by reading a position.json sentinel that backtest
never sets → byte-identical offline.

---

## Tasks (dependency order)

### Step 1 — ASCII-safe broker_recon logging; drop the headed/placeholder reader (Req 1)
- In `broker_recon/reader.py`, `broker_recon/reconcile.py`, `broker_recon/tradovate_login.py`,
  replace every `→` in **printed/logged strings** with ASCII `->` (the crash sites are
  `reader.py:90, 95, 110` inside `_warn(...)` f-strings). Sweep the `→` in comments/docstrings
  too (cheap, prevents future copy-into-print regressions): `reader.py:25,45,65,118`,
  `reconcile.py:8,9,49,117,271,299`, `tradovate_login.py:20`. Pure text change.
- Do NOT delete `reader.py` (test_reconcile / reconcile_after_entry still import it), but the
  NEW close-reconcile must NOT use the headed `TradovateOrderReader`/placeholder DOM selectors.
  The headed reader stays only for the legacy `reconcile_after_entry` path (unchanged).

### Step 2 — New headless broker-state helper (Req 2)
Create `broker_recon/broker_state.py`:
- `fetch_broker_state(symbol: str) -> dict | None` — headless Playwright, reusing
  `login_and_select_account` and the same Orders-blotter read approach as
  `reports/get_tradovate_orders.py` (headless except where Tradovate forces headed; match
  whatever `get_tradovate_orders.run()` uses — it launches `headless=False`, so use the SAME
  launch to be safe, but DO NOT download CSV; read today's Orders rows in-process). Reads
  `.env` creds (`TRADOVATE_USERNAME/PASSWORD`, `TRADING_ACCOUNT_IDS`).
- Returns a dict: `{"net_position": int, "avg_entry": float, "stop_price": float|None,
  "direction": "long"|"short"|"flat"}` where
  `net_position = Σ(filled buys) − Σ(filled sells)` for `symbol`; `direction` derived from sign;
  `avg_entry` = size-weighted avg of the filling fills; `stop_price` = the working protective
  stop on the correct side (reuse `reconcile._is_protective_stop_row` logic).
- **Detection only** — NEVER sends a broker order.
- **Degrades to "unknown":** returns `None` on ANY failure (missing creds, login/DOM/timeout,
  account-gone). Wrap the whole body in try/except; never raise. ASCII-only logging.
- Keep DOM parsing thin and behind a function so unit tests inject a fake (see Step 7); the
  selector confirmation is a supervised smoke step, not a unit test.

### Step 3 — Reconcile-on-close core (pure, testable) (Req 4)
Create `broker_recon/recon_on_close.py` with a PURE decision function + an impure applier:
- `decide_correction(strat_active: dict, broker: dict|None) -> dict` — pure, no I/O:
  - `broker is None` (unknown) → `{"action": "noop", "reason": "broker-unknown"}` (degrade:
    trust strategy; preventive layers remain primary).
  - strat flat + broker flat → `{"action": "noop", "reason": "confirmed-flat"}`.
  - strat flat + broker has position N → `{"action": "adopt", "direction", "size": N,
     "avg_entry", "stop"}`.
  - strat long/short + broker flat → `{"action": "suppress_close", "reason": "broker-flat"}`.
  - strat long N + broker M (same dir, N≠M) → `{"action": "resize", "size": M}`.
  - strat dir != broker dir (both non-flat) → treat as `adopt` to broker truth.
- `apply_correction(decision, *, intended_close_event) -> bool` — impure, live-only; returns
  `True` iff the pending close-MKT must be SUPPRESSED by the caller. Uses
  `live_orders.get_position/_save_pos/_log` and `smt_state`:
  - **adopt:** restore `pos["active"]` from broker truth — `direction`, `contracts`=size,
    `fill_price`=avg_entry, `stop`=working broker stop (fallback to a `valid_stop_for_fill`
    floor when broker stop unknown); REVERT the phantom close's side effects by decrementing
    `failed_entries` and `cautious_dist_shrinks` by 1 each (floor at 0) IF the close that
    triggered this was a `stopped-out`/same-bar-stop (those are the only paths that ++ them,
    `session_pipeline.py:1545-1546`); re-arm the cautious ladder from the live hypothesis via
    `smt_state.freeze_active_mgmt(pos["active"], direction, smt_state.load_hypothesis())`;
    set sentinel `pos["recon_suppress_force_entry"] = True` (consumed in Step 6); `_save_pos`;
    emit `{"kind":"recon-adopt", ...}`. Return `True` (suppress the close — there IS a position,
    we just adopted it; the close event was a phantom).
  - **suppress_close:** set `pos["active"]={}` and clear `stop_entry`/`stop_direction`/
    `conf_bar_entry` (mirror `close_position` state clears) WITHOUT dispatching a broker order;
    `_save_pos`; emit `{"kind":"recon-flat", ...}`. Return `True` (suppress the close-MKT — no
    position to sell into).
  - **resize:** set `pos["active"]["contracts"]=M`; `_save_pos`; emit `recon-resize`.
    Return `False`.
  - **noop:** return `False`.
- ASCII-only logging; never raises (wrap impure body, log+return `False` on error so the close
  proceeds normally — failing safe = do what the strategy intended).

### Step 4 — Wire the SYNCHRONOUS close-reconcile into dispatch (Req 3 + Req 5)
In `live_orders.py`, add a single guarded helper:
```
def _reconcile_on_close(close_event: dict) -> bool:
    """Live-only synchronous close reconcile. Returns True iff the pending broker
    close must be SUPPRESSED. No-op (returns False) outside live mode or on any failure."""
    if not _LIVE:
        return False
    try:
        from broker_recon import broker_state, recon_on_close
        symbol = os.environ.get("TRADING_SYMBOL", "MNQ1!")
        broker = broker_state.fetch_broker_state(symbol)   # None on failure → noop
        strat_active = _load_pos().get("active") or {}
        decision = recon_on_close.decide_correction(strat_active, broker)
        return recon_on_close.apply_correction(decision, intended_close_event=close_event)
    except Exception as exc:  # pragma: no cover
        print(f"[live_orders] close reconcile failed (ignored): {exc}", flush=True)
        return False
```
Call it SYNCHRONOUSLY at the top of each close handler in `dispatch`, BEFORE the broker action
and BEFORE state is cleared:
- `market-close` (`live_orders.py:734`): after the `_pending_close_after` deferral check but
  before `close_position(...)` — `if _reconcile_on_close(sig): _log(sig); return`.
- `stop-exit` (`live_orders.py:748`): before `_executor.place_close("close")` —
  `if _reconcile_on_close(sig): _log(sig); return`.
- `stopped-out` (`live_orders.py:839`): before clearing `active` — `if _reconcile_on_close(sig):
  _log(sig); return` (ADOPT restores active; SUPPRESS leaves flat which the existing clear would
  also produce — but emit the recon event for the audit trail).
Synchronous because the post-close cooldown / re-entry happens on the SAME or NEXT bar in the
same thread; running inline guarantees that by the time any entry fires the broker is reconciled.
The `fetch_broker_state` call is bounded (set a short Playwright timeout, e.g. 15s) so it can't
hang the loop indefinitely; on timeout it returns `None` → noop (fail-safe).

### Step 5 — Gate immediate-commit entries on the close-reconcile (Req 5)
- **Entry-STOP (`new-stop-entry`)**: NO change — a resting STP is harmless; if the broker is
  still in the prior position the resting order just sits until triggered, and Step 4 already
  reconciled at the close that preceded it.
- **`market-entry`**: the close that precedes a normal re-entry already ran Step 4 synchronously
  (the strategy emits `market-close`/`stopped-out` → dispatch → `_reconcile_on_close` →
  guaranteed-flat-or-adopted before the entry bar). For the **direction-flip same-bar** case
  (`market-close`→`market-entry` with `flatten_first=True`, no cooldown gap), Step 4's
  synchronous reconcile on the `market-close` runs between the close and the paired entry within
  the same `dispatch`/bar sequence, so the flip inherits the guarantee. Add a belt-and-suspenders
  guard at the `market-entry` handler (`live_orders.py:727`): if `_reconcile_on_close` ADOPTED
  on the immediately-preceding close (i.e. `pos.get("recon_suppress_force_entry")` is set), SKIP
  the market entry and clear the sentinel (do NOT stack onto an adopted live position).
- **STP→MKT downgrade** (inside `place_stop_entry`, `live_orders.py:287-292`): this is an ENTRY
  not a close; it is only reached when the strategy believed it was flat. The close that made it
  flat already ran Step 4. No new reconcile call here (leave the existing GIL-36
  `_spawn_reconcile` after-entry verify as-is). Document this reasoning inline.

### Step 6 — Suppress the pending re-entry on ADOPT (Req 4 bullet "SUPPRESS the pending re-entry")
The pipeline's `_force_entry_eval_after` lives on `SessionPipeline`, unreachable from `dispatch`.
ADOPT already restores `pos["active"]`, and the force-eval entry path runs `run_strategy` which
will not open a NEW entry while a position is active — so the primary suppression is automatic.
Add a minimal sentinel honor in the pipeline to also DISARM the force-eval (prevents a spurious
re-eval after the adopted position later closes):
- In `session_pipeline.py`, at the very top of the force-eval block (`session_pipeline.py:1478`,
  just before computing `_force_eval_now`), add:
  ```
  if _smt_state.load_position_ro().get("recon_suppress_force_entry"):
      self._force_entry_eval_after = None
      self._force_market_entry = False
      _p = _smt_state.load_position(); _p.pop("recon_suppress_force_entry", None)
      _smt_state.save_position(_p)
  ```
- This is byte-identical offline: backtest never sets `recon_suppress_force_entry` (only
  `apply_correction` does, which is live-only). The read is a cheap no-op key check.

### Step 7 — Tests (Req: fake reader, no browser) — `tests/test_reconcile_on_close.py`
Mirror `tests/test_reconcile.py` isolation fixtures (`_isolate_global_dir`, `_reset_state_dir`,
`_session`, `_read_events`). Inject broker state by monkeypatching
`broker_recon.broker_state.fetch_broker_state` to return a fixture dict (or `None`), and force
`live_orders._LIVE = True` for the duration. Test cases:
- `test_decide_*` (pure `decide_correction`): flat/flat→noop; flat/broker-long-N→adopt(N);
  long/broker-flat→suppress_close; long-N/broker-M-samedir→resize(M); broker-unknown(None)→noop;
  dir-mismatch→adopt.
- **(a)** `test_phantom_close_broker_long_adopts_and_suppresses_reentry`: seed `active={}` (phantom
  flat) + `failed_entries=1`, `cautious_dist_shrinks=1`; broker = long size 2 @ entry E, stop S;
  dispatch a `stopped-out` sig → assert `_reconcile_on_close` returned True (close suppressed),
  `pos["active"]` restored (direction=long, contracts=2, fill_price=E, stop=S),
  `failed_entries==0` and `cautious_dist_shrinks==0` (reverted), cautious ladder frozen
  (mgmt fields present), `recon_suppress_force_entry==True`, and a `recon-adopt` event in
  events.jsonl. Then assert a subsequent `market-entry` dispatch is SKIPPED (no `place_entry`
  call) because the sentinel is set, and the sentinel is cleared.
- **(b)** `test_broker_flat_strategy_long_suppresses_close_mkt`: seed `active={long}`; broker=flat;
  dispatch `market-close` → assert `_executor.place_close` NOT called (close-MKT suppressed),
  `pos["active"]=={}`, a `recon-flat` event emitted.
- **(c)** `test_entry_stop_path_untouched`: dispatch `new-stop-entry` → assert
  `_reconcile_on_close` is NOT invoked (patch it with a MagicMock and assert `not called`) and
  the resting STP placement happens normally.
- **(d)** `test_market_entry_and_downgrade_gated_on_reconcile`: (i) `market-entry` after an ADOPT
  (sentinel set) is skipped; (ii) `market-entry` with no sentinel proceeds normally; (iii) the
  STP→MKT downgrade path does NOT call `_reconcile_on_close` (it is an entry, not a close) —
  assert via MagicMock that the close-reconcile is not invoked on `place_stop_entry`'s downgrade.
- `test_broker_unknown_is_noop`: `fetch_broker_state` returns `None` → close proceeds exactly as
  today (no adopt/suppress; `_reconcile_on_close` returns False).
- `test_offline_inert`: with `live_orders._LIVE = False`, `_reconcile_on_close` returns False
  without importing `broker_state` (assert no fetch). Guards the byte-identical-regression claim.

Also extend `tests/test_reconcile.py` with `test_warn_strings_ascii_only` (or a small test that
imports the three broker_recon modules and asserts no `→` in any module's source / printed
helper) to lock the charmap fix.

### Step 8 — Verification
- `pytest tests/test_reconcile_on_close.py tests/test_reconcile.py` green.
- Full suite: `pytest` — no NEW failures vs the documented baseline.
- 1s A/B on 2026-06-22 (or another live date): baseline vs change must be **byte-identical**
  trades (P&L + count). The reconcile path is live-only and the only pipeline touch (Step 6) is
  guarded by a key backtest never writes.

---

## Acceptance criteria
1. No `→` remains in any printed/logged string in `broker_recon/` (charmap crash fixed); the
   three modules import cleanly and a guard test asserts ASCII-only.
2. `broker_recon/broker_state.fetch_broker_state` reuses `login_and_select_account`, reads the
   Orders blotter headlessly, returns net position + working stop, NEVER sends an order, and
   returns `None` (degrade) on any failure without raising.
3. The close-reconcile runs SYNCHRONOUSLY inside `live_orders.dispatch()` for `market-close`,
   `stop-exit`, and `stopped-out`, BEFORE the broker close action / state clear, so that whenever
   an entry order is fired the broker is flat (or has been adopted).
4. Bidirectional correction works: flat/flat→noop; flat/broker-N→adopt (active restored with
   entry/size/stop, phantom side-effects reverted, cautious ladder re-armed, pending re-entry
   suppressed, `recon-adopt` emitted, NO broker order); long/broker-flat→flat + close-MKT
   suppressed (`recon-flat`); size mismatch→adopt broker size.
5. Entry-STOP path is untouched; `market-entry`, STP→MKT downgrade, and the same-bar direction
   flip only commit after the close-reconcile confirmed flat (or were skipped on adopt).
6. ALL new behavior is live-only: `_LIVE`-gated and reachable only via `dispatch`; 1s A/B on a
   live date is byte-identical to baseline.
7. New unit tests (a)-(d) + pure `decide_correction` cases + offline-inert + broker-unknown all
   pass with a fake reader and no browser.
