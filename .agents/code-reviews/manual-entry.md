# Code Review: manual-entry

**Branch:** autoresearch/agents

**Stats:**
- Files Modified: 3
- Files Added: 0
- Files Deleted: 0
- New lines: ~212
- Deleted lines: ~130

---

## Issues Found

---

```
severity: high
file: C:\Users\gilad\projects\auto-co-trader\agents\trade.py
line: 150
issue: _force_hypothesis_for_direction called even when --force bypasses an active position
detail: When the user runs `trade.py up --force` with an active position, the guard at line 145
        is skipped (force=True), so _force_hypothesis_for_direction fires unconditionally. Inside
        that function, cancel_stop_entry(reason="direction-override") is called even though there
        may be no pending stop — that's a no-op and harmless. However, build_hypothesis_from_direction
        is then called with skip_position_reset=True, which means it will still execute
        `position["failed_entries"] = 0` whenever old_direction == "none". If there is an active
        position in the opposite direction (the scenario that requires --force), overwriting the
        hypothesis underneath it without also closing the position creates an inconsistent state:
        the hypothesis says "up" but position.json still has an active "short" trade. The pipeline
        will then manage the new "up" hypothesis but run_trend() is watching an existing short
        position — this is a latent inconsistency.
        The plan doc scopes _force_hypothesis_for_direction to "when forced_v2 differs from the
        current hypothesis direction" only, not to "when there is an active position in the old
        direction". If --force is used over an active position, the function should guard against
        this case or be a documented out-of-scope scenario. As written, the function is called
        unconditionally before the active-position check path.
suggestion: Add a guard at the top of _force_hypothesis_for_direction (or at the call site) to
        return early if pos.get("active") is truthy, since rewriting the hypothesis under an
        active position is dangerous:
            if hyp.get("direction", "none") == forced_v2:
                return
            pos = get_position()  # already loaded in trade.py — could pass it in
            if pos.get("active"):
                return  # active position present; do not rewrite hypothesis
        Alternatively, document in the plan that --force with an active position is unsupported
        and add a print+return guard inside _force_hypothesis_for_direction itself.
```

---

```
severity: medium
file: C:\Users\gilad\projects\auto-co-trader\agents\hypothesis.py
line: 1179
issue: save_hypothesis called before position reset in build_hypothesis_from_direction, but hyp_event reports the post-veto direction; return value uses veto-mutated direction
detail: When skip_veto=False (the pipeline path), the veto block at line 1104 can mutate the
        local `direction` variable to "none". The function then saves hypothesis.json with
        direction="none" (correct), and returns `divs` (line 1180) when direction=="none".
        However the hyp_event dict at line 1163 uses `direction`, `weekly_mid`, `daily_mid` etc.
        after the veto mutation — this is correct and matches the original behavior.
        The new risk introduced by the extraction is that `old_direction` passed in from
        run_hypothesis() is the value BEFORE any veto, while in the original inline code,
        old_direction was also pre-veto. This invariant is preserved. No regression here, but
        worth noting: the `formed_at` logic at line 1127 compares the post-veto `direction`
        against `old_direction`. If veto fires (direction becomes "none") and old_direction was
        already "none", formed_at gets old_formed_at (or now if empty) — correct. If veto fires
        and old_direction was "up", direction="none" != old_direction="up" → formed_at = now.
        This is a behavior change from the original code, which only ran this block when
        direction != old_direction at the beginning (before veto). After veto, the original code
        would set formed_at = now for the "none" hypothesis, same as new code. No regression.
        No actionable bug, but the formed_at/veto interaction warrants a comment.
suggestion: Add a brief inline comment above the formed_at block explaining that direction here
        is post-veto, so a vetoed direction always gets a fresh formed_at. This aids future
        readers of the extracted function.
```

---

```
severity: medium
file: C:\Users\gilad\projects\auto-co-trader\agents\live_orders.py
line: 563
issue: _liq_map comprehension silently drops FVG entries — mid label may be empty even when liquidities contain week_high/week_low as FVGs
detail: The comprehension at line 563 filters on l.get("kind") == "level". If daily.json stores
        week_high or week_low as kind="fvg" (e.g. a FVG that spans the week high), they will be
        excluded and wh/wl will be None, producing weekly_mid="". The veto is skipped
        (skip_veto=True) so this does not gate the direction, but the written hypothesis.json will
        have weekly_mid="" which is what run_trend() and _mid_cross_guard read. An empty label
        means the mid-cross guard never triggers, which is a silently degraded state.
        The existing run_hypothesis() path reads these values from compute_live_hl_mid() which
        does its own H/L computation from bar data — it does not rely on daily.json kind tags.
        This divergence in the mid-label sourcing paths means the manual-entry path could produce
        a different (weaker) weekly_mid than the pipeline path.
suggestion: The plan acknowledges this tradeoff explicitly ("daily.json values are good enough").
        However, the level-only filter is stricter than documented. Either:
        (a) extend the comprehension to also handle kind="fvg" by reading (top+bottom)/2 as the
            representative price for the name, or
        (b) add a fallback comment making the intentional limitation explicit so the silent empty
            is not confused with a data-availability gap.
        Minimum fix: add a comment: "# Only 'level' kind carries a single price; FVG-keyed
        week_high/week_low are rare but would produce empty mid labels here."
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\agents\hypothesis.py
line: 1066
issue: old_formed_at parameter is not part of the plan spec signature but is accepted — minor contract divergence
detail: The plan's acceptance criteria (section "Functional", bullet 1) specifies the exact
        14-parameter signature for build_hypothesis_from_direction. The implemented function adds
        a 15th keyword-only parameter old_formed_at: str = "". This is a correct and necessary
        addition (run_hypothesis() passes hypothesis.get("formed_at", "") to preserve formed_at
        continuity), but the acceptance criteria technically fails on "exact signature" wording.
        No functional bug, but the plan acceptance criteria should be updated to include
        old_formed_at in the signature spec to avoid confusion during future validation.
suggestion: Update the acceptance criteria bullet in .agents/plans/manual-entry.md to include
        old_formed_at: str = "" in the listed signature.
```

---

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\agents\live_orders.py
line: 569
issue: build_hypothesis_from_direction call site in _force_hypothesis_for_direction does not pass old_formed_at
detail: When _force_hypothesis_for_direction calls build_hypothesis_from_direction, it omits
        old_formed_at (defaults to ""). If forced_v2 == old_direction (would have returned
        early), this is fine. But if the direction changes, the formed_at will always be set
        to pd.Timestamp(now).isoformat() because direction != old_direction — so old_formed_at
        is irrelevant in this branch. However, for robustness and to match the contract, passing
        old_formed_at=hyp.get("formed_at", "") explicitly would be cleaner and self-documenting.
suggestion: Add old_formed_at=hyp.get("formed_at", "") to the build_hypothesis_from_direction
        call in _force_hypothesis_for_direction for clarity, even though it doesn't change
        behavior in the direction-change path.
```

---

## Summary

The refactor is structurally sound: the extraction of steps 7–11 into `build_hypothesis_from_direction` is clean, the delegation from `run_hypothesis()` is correct, and the rename of `_compute_mid_label` is complete with no stale references. Both modules import cleanly.

The high-severity finding is the missing guard against calling `_force_hypothesis_for_direction` when an active position already exists (reachable via `--force`). The medium-severity finding is a potential silent empty `weekly_mid`/`daily_mid` in the manual path due to the level-only filter on `_liq_map`.
