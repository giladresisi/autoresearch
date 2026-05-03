# Code Review: update_pickmytrade

**Stats:**
- Files Modified: 5 (execution/pickmytrade.py, execution/protocol.py, execution/simulated.py, automation/main.py, tests/test_pickmytrade_executor.py)
- Files Added: 0
- Files Deleted: 0
- New lines: +317
- Deleted lines: -55

---

## Issues Found

---

```
severity: high
file: execution/pickmytrade.py
line: 139–144
issue: place_close adds close orders to _pending, polluting the fill-tracking queue
detail: _post_order always adds the order to _pending on success (line 179–181). place_close passes
        ctx = {"label": label}, which has no "direction", "order_type", or "requested_price" keys.
        When the fill-poll loop eventually dequeues this entry and calls _query_fill, it will either:
        (a) endlessly retry a fills-endpoint lookup for a close order that will never appear there
            (confirmed by the design notes: "close orders do not produce a queryable fill via the PMT
            fills endpoint"), and
        (b) raise a KeyError on ctx["direction"] if the fills endpoint ever returns status="filled"
            for any order, crashing the poll thread silently (caught by the broad except in _query_fill,
            but logging a spurious [FILL-WARN] on every poll interval until the process restarts).
        place_close is meant to be fire-and-forget without fill tracking, yet it feeds _pending.
suggestion: After the _post_order call in place_close, immediately remove the generated order_id from
            _pending, or restructure _post_order to accept an optional skip_pending flag:
                def place_close(self, label: str = "close") -> None:
                    order_id = f"pmt-{uuid.uuid4().hex[:8]}"
                    payload = self._build_payload("close")
                    ctx = {"label": label}
                    self._post_order(order_id, payload, ctx)
                    with self._lock:
                        self._pending.pop(order_id, None)  # close orders are not queryable
```

---

```
severity: medium
file: automation/main.py
line: 616, 630, 632, 691, 693
issue: _executor called without None guard — AttributeError if invoked outside main()
detail: _executor is declared as PickMyTradeExecutor | None = None at module level (line 74) and is
        only assigned in main(). All five new executor call sites in _process_scanning dereference it
        directly without a None check. The existing call sites (lines 786, 807) have the same pattern
        and are pre-existing; however, the new lifecycle_batch and limit_placed/limit_moved paths
        (lines 616, 630, 632) are reached by a wider set of code paths (including tests that call
        _process_scanning directly with a mocked scanner). If a test or future caller exercises
        _process_scanning before main() initialises _executor, the process will raise AttributeError
        rather than giving a clean error.
suggestion: Add a guard at the top of _process_scanning (or immediately before each new executor call
            site) consistent with how the codebase handles _ib_source: 
                if _executor is None:
                    return
            Alternatively, since all existing exit-path calls lack this guard too, document in a
            comment that _process_scanning must only be called after main() has run.
```

---

```
severity: low
file: execution/pickmytrade.py
line: 125–132
issue: place_stop_after_limit_fill: quantity=0 overrides top-level quantity but multiple_accounts
       quantity_multiplier remains 1, leaving the per-account quantity field semantically inconsistent
detail: _build_payload sets quantity=self._contracts in the base dict, then payload.update(extra)
        overrides it with quantity=0. The multiple_accounts[0]["quantity_multiplier"] stays at 1.
        Per PMT docs, same_direction_ignore+pyramid=False+quantity=0 is the SL-update pattern,
        so the top-level quantity=0 is correct. However, if PMT uses quantity_multiplier to scale
        the top-level quantity (0 * 1 = 0), this is a no-op and fine. If PMT ignores quantity_multiplier
        when quantity=0, it is also fine. The risk is that PMT could interpret quantity_multiplier=1
        combined with quantity=0 differently from its documented SL-update semantics. No PMT
        documentation was available to confirm this edge case.
suggestion: Explicitly set quantity_multiplier=0 in the extra kwargs passed to _build_payload for
            the SL-attach case to make the intent unambiguous:
                payload = self._build_payload(
                    data,
                    quantity=0,
                    sl=float(position["stop_price"]),
                    update_sl=True,
                    pyramid=False,
                    same_direction_ignore=True,
                )
                payload["multiple_accounts"][0]["quantity_multiplier"] = 0
            Or pass it inline by rebuilding multiple_accounts in _build_payload when quantity==0.
            If PMT docs confirm quantity_multiplier is irrelevant for SL-only updates, add a comment
            noting this.
```

---

```
severity: low
file: tests/test_pickmytrade_executor.py
line: 386–398
issue: test_modify_limit_entry_sends_close_then_limit does not assert ordering is guaranteed
detail: The test calls _drain(ex) after modify_limit_entry and then checks call_args_list[0] is
        "close" and call_args_list[1] is "buy". The close step runs synchronously so it will always
        be call index 0. However, the test does not assert call_count == 2 before indexing
        call_args_list, meaning if a future refactor adds a third HTTP call the assertion on index 1
        might pass against the wrong call.
suggestion: Add assert ex._http.post.call_count == 2 before the call_args_list indexing (already
            present at line 393 — this is already correctly done). No code change needed; this is a
            documentation note only.
```

---

## Pre-existing Failures

```
test: tests/test_automation_main.py::test_main_validates_env_vars_before_start
status: FAILED (pre-existing — confirmed by git stash + re-run before this changeset)
root cause: The test expects RuntimeError when PMT_WEBHOOK_URL/PMT_API_KEY are absent, but the test
            environment has these env vars set (likely from a .env file loaded via load_dotenv() at
            module import time), so the validation never fires.
introduced by: Not introduced by this changeset.
```
