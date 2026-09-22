# Code review: plan 40, unnested HTF extremes as T2 pools (unstaged)

**Stats:**

- Files Modified: 14
- Files Added: 6 (plan + 5 code/test; regression/htf-ab/ scratch excluded)
- Files Deleted: 0
- New lines: ~1894 (500 tracked + 1394 new files)
- Deleted lines: 25

Tests run: `agent/facts/test_htf_extremes.py test_htf_source.py`, `agent/test_menus.py`,
`agent/trader/test_target_at_fill.py test_gate_mechanisms.py test_gate_no_legacy_writes.py`,
`tests/test_agent_dispatch.py`: 379 passed, 2 failed, 4 deselected (slow).

Verified correct: no lookahead (`_window` is `lo <= ts < hi`; `_read` cuts `< as_of`; seed
and pruning use `[as_of, now)`); trade date = ts+7h; ISO week of the trade date (Sunday
18:00 goes to Monday's week, and the ISO year is handled); strictly-beyond nesting through
the suffix max/min over the rows after the extreme bar; dedupe month>week, then most recent.
`_dol_menu` and `select_target` are byte-identical when `extra_pools` is None/[] or the
flag is off. Live load failure is non-fatal. No live_orders import, no legacy state-file
writes, and no wall clock in the Executor. The only new prints are the `[AGENT-LIVE]` lines
in automation/main.py.

## Pre-existing failures

- `agent/test_menus.py::test_dol_projection_stretch_gated` and
  `::test_dol_projection_weekly_extension_gate_direction_aware` expect the stretch gates to
  be armed. HEAD `e65e4fa` set `DOL_PROJECTION_STRETCH_GATES_ARMED = False` and this diff
  does not touch it. These are not regressions from plan 40, but the tests should be
  updated or marked xfail in the O8 change.

## Issues

severity: medium
file: agent/facts/htf_source.py
line: 462-467 (prior_session_close) / 497
issue: The staleness guard treats a full CME holiday as a missing session, so the feature silently drops out on the next trade date.
detail: The guard walks back weekends only. For trade date Mon 2026-12-28 the "prior close" is Fri 12-25 17:00 (Christmas, CME closed), so `need` = 12-25 11:00. The last real bar is Thu 12-24 ~13:15, so HtfSourceStale is raised. The same happens on Mon 2027-01-04 (Jan 1 is a Friday) and on the Monday after Good Friday. Live prints "unavailable" and T2 falls back to the old selection. Replay does the same and records only an error artifact, so an A/B spanning those dates scores B1 == A without anyone noticing.
suggestion: Measure staleness against the last weekday that is not a full CME closure (a small holiday set: Good Friday, Dec 25, Jan 1), or relax the guard to "last bar >= as_of - N days" with N covering weekend+holiday (e.g. 4 days) and keep the per-session precision for ordinary weekdays. Add a test for 2026-12-28.

severity: medium
file: agent/trader/replay.py
line: 113-120 (with regression/htf-ab/ab_one.py:74)
issue: A failed HTF load in replay is invisible to the caller, so a flag-ON arm can silently run as arm A.
detail: `build_replay_trader` swallows every exception and writes only an error artifact. `run_replay`'s result does not carry it. `ab_one.py` reports `htf_artifact: True` from `os.path.exists(...)`, which is also true for the ERROR artifact. So an arm-B1 date whose load failed (holiday staleness, missing era file, bad ledger) shows zero delta, and the pre-registered adoption rule (sum delta >= 0, "zero unexplained deltas") reads that as neutral evidence.
suggestion: Surface the status in `run_replay`'s per-date result (e.g. `res["htf"] = "ok"|"error: ..."`), and in ab_one use `htf_source.read_artifact(run_dir) is not None`. Mark such cases invalid for the B arms instead of scoring them.

severity: medium
file: agent/facts/htf_extremes.py:1, agent/trader/target.py:504, agent/derive_facts.py:13 (comments cite "l2-target-selection.md §7a")
issue: The spec entry the code cites does not exist. Plan deliverable 1 / Wave 0.1 is not done.
detail: `l2-target-selection.md` and `l2-mechanisms.md` are unchanged; the only HTF mention is the §6 "HTF FVG edges" row. The project CLAUDE.md makes the doc the specification and `docs/entry-mechanism-change-protocol.md` puts the rule/CANDIDATE entry before the code. Meanwhile the 09-21 figures are already pinned in `named_cases.py` (L656-720), which the plan reserves for the Wave-4 adoption commit.
suggestion: Add the §7a entry marked "flag OFF pending A/B" with the evidence and the pre-registered decision rule, plus the §11 pointer row, before committing. Alternatively mark the named_cases block as a CANDIDATE/pre-adoption figure set so it is not read as an adopted era.

severity: low
file: agent/trader/graft.py
line: 351-358
issue: `executor_context(...)` runs outside the try, so a malformed `htf_extremes` would raise in `TraderGraft.__init__` and turn into a live REFUSED start.
detail: Today the input always comes from `load_session_extremes`, so this is latent. But the stated contract ("failing ... must not cost the session its trader") only covers the artifact write. For example, missing `meta.as_of` gives `pd.Timestamp(None)` and then `.tz_convert`.
suggestion: Put `self._htf_ctx = executor_context(...)` in the same try and fall back to None on failure.

severity: low
file: automation/main.py
line: 1098-1103 / tests/test_agent_dispatch.py
issue: The existing real-build `_build_trader` tests now read the REAL `<global>/general/live/{MNQ,MES}_1m.parquet` (~18 MB each).
detail: Every test that calls `_real_build` without patching `load_session_extremes` now depends on machine-global live data. It either gets a list or prints "unavailable", which is harmless for assertions but slow and non-hermetic. This is the same class of problem as the "tests clobber live global" memory, though read-only here.
suggestion: In the `_wired` fixture, autouse-patch `htf_source.load_session_extremes` to raise or return None, or point `ACT_GLOBAL_DIR` at tmp.

severity: low
file: agent/trader/replay.py
line: 113-116
issue: Every replay, flag off included, reads both full main-era 1m parquets (MES is audit-only) per date.
detail: This adds I/O to every default-suite and `-m slow` replay for zero behavioural effect while `HTF_EXTREMES_IN_T2 = False`. CLAUDE.md asks that the default suite stay under a minute.
suggestion: Acceptable for artifact parity. If the slow-suite time grows, gate the load on the flag or load MNQ only in replay.

severity: low
file: automation/main.py
line: 1098-1103
issue: A live load failure leaves no artifact in the session folder, while replay writes an error artifact.
detail: Post-session analysis can't tell from the folder alone whether HTF was absent because of a failure or because of a pre-plan-40 build. Only the stdout line records it.
suggestion: Call `write_error_artifact(out_dir, date, "live", reason)` in the except branch, wrapped in its own try. The test at test_agent_dispatch.py asserting the artifact does NOT exist would need to change accordingly.

severity: low
file: agent/facts/htf_extremes.py
line: 228-237
issue: NaN High/Low rows poison `np.maximum.accumulate` / `np.minimum.accumulate`, so no level before a NaN bar can ever be nested.
detail: NaN propagates through the reversed accumulate, which makes `after_hi[i] > price` False for every earlier i. Unlikely in validated 1m parquets, but the module otherwise promises "never raises / degenerate input".
suggestion: Drop rows with NaN High/Low in `_ohlc` (`df = df.dropna(subset=["High","Low"])`).

severity: low
file: agent/trader/target.py
line: 595-597
issue: `htf_error` is lost when the menu is empty (`return None`), so a failure on a no-target fill is not recorded.
suggestion: Accept as is, or have the Executor record the error separately. This is minor while the flag is off.
