# Pre-implementation baseline (global-path-restructure)

Captured before any code changes. Command:
`uv run python -m pytest tests/ -q --ignore=tests/test_ib_realtime.py`

**Result: 27 failed, 1012 passed, 6 skipped, 14 deselected.**

`tests/test_ib_realtime.py` excluded: `test_gap_fill_not_called_from_start` hangs on a
real IB retry loop (sleep(20) loop); pytest-timeout's thread method cannot kill it on
Windows, force-terminating the process before later tests run. Run that file separately
deselecting that one test.

## Pre-existing failures (NOT introduced by this work — excluded from acceptance)

Verified sample are unrelated to paths (stale-mock drift, pure-logic slippage asserts,
test-isolation flake):

- test_automation_main.py::test_v2_session_end_closes_active_position
- test_automation_main.py::test_v2_session_end_cancels_pending_entry
- test_check_session_parquets.py::TestProcessInstrumentSessionEnd::test_ok_session_merges_and_backs_up
- test_check_session_parquets.py::TestProcessInstrumentSessionEnd::test_minor_session_merges_as_is
- test_check_session_parquets.py::TestProcessInstrumentSessionEnd::test_late_start_escalates_to_rebuild
- test_hypothesis_smt.py::test_compute_direction_case_1_3_long
- test_hypothesis_smt.py::test_compute_direction_case_1_3_short
- test_hypothesis_smt.py::test_compute_direction_case_1_2
- test_hypothesis_smt.py::test_compute_direction_case_1_4
- test_hypothesis_smt.py::test_compute_direction_case_1_1
- test_hypothesis_smt.py::test_compute_direction_case_1_5
- test_hypothesis_smt.py::test_generate_writes_hypothesis_file
- test_hypothesis_smt.py::test_generate_api_failure_falls_back_to_rule_engine
- test_orchestrator_main.py::test_main_session_dirs_created   (stale mock: grace_end_dt)
- test_orchestrator_main.py::test_pre_session_init_skips_when_no_api_key
- test_pickmytrade_executor.py::test_pmt_market_entry_long_slippage
- test_pickmytrade_executor.py::test_pmt_market_entry_short_slippage
- test_pickmytrade_executor.py::test_pmt_zero_slip_ticks_mkt_still_applies_1tick
- test_pickmytrade_executor.py::test_modify_stop_entry_includes_sl
- test_pickmytrade_executor.py::test_modify_stop_entry_sends_close_then_stop
- test_pickmytrade_executor.py::test_modify_stop_entry_close_is_synchronous
- test_pickmytrade_executor.py::test_pmt_stop_entry_after_1100_applies_2tick_slippage
- test_pickmytrade_executor.py::test_modify_stop_entry_replaces_even_if_close_fails
- test_session_pipeline.py::test_on_session_start_writes_levels_json   (passes in isolation; full-run flake)
- test_session_pipeline.py::test_on_session_start_levels_json_rewritten_on_restart
- test_smt_humanize.py::test_s1_human_slippage_applied_to_long_entry
- test_smt_humanize.py::test_s2_human_slippage_applied_... (s2)

## test_ib_realtime.py pre-existing failures (excluded from original baseline run)
The original baseline `--ignore`d this file (one test hangs on a real IB retry loop).
Running it with that one test deselected yields 4 PRE-EXISTING failures (ib_realtime.py is
byte-identical to HEAD; the tests import only data.ib_realtime + pandas/mock — IB-mock/
connectivity inherent, not caused by this refactor):
- test_gateway_disconnect_raises_ibgateway_disconnected_error
- test_ibgateway_disconnected_error_not_retried
- test_gap_fill_1s_ib_skips_when_already_current
- test_1s_dfs_freed_after_gap_fill_in_start
Plus test_gap_fill_not_called_from_start (hangs — always deselect on Windows).

## Environment note — no production data in this worktree
No `data/*.parquet`, no `data/regression/<date>/`, no 1s cache, no `data/*.json` exist here
(gitignored; live in the main worktree / global location). Consequences:
- Equivalence gate (Task 5.3) cannot diff against a real pre-move baseline. Implemented
  instead as a LOCATION-INDEPENDENCE test: identical synthetic inputs run into two distinct
  ACT_GLOBAL_DIR/ACT_REGRESSION_DIR locations must yield byte-identical events/trades ledgers.
  Same invariant ("path refactor is output-neutral"), fully automatable.
- Migration dry-run reports nothing to migrate here (correct).

## Scope decision
Plan-named files + actual live/backtest consumers. OUT OF SCOPE: legacy v1
(automation/main.py, signal_smt.py), ad-hoc analysis scripts (_compare30d.py, _compare4way.py,
_filter_analysis.py, _loss_limit_1s.py, _max_drawdown.py, _trace_failed_entries.py,
_tmp_deep_analysis.py, analyze_deltas.py, plot_comparison.py), and scripts/* maintenance
utilities except scripts/check_session_parquets.py (Task 3.1).
