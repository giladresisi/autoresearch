# Cycle-1 shadow runbook

How to run the Analyzer → Planner → Executor chain beside the legacy engine, with the
broker detached, and produce the comparison artifact afterwards.

Cycle 1 places **no orders**. The chain stops at a structured
`trader_decisions.jsonl` record: *this is where I would have entered, and here is every
entry I refused and why.*

## Configuration

```
LIVE_TRADING=true      # keeps automation.main (the real live path)
DISCONNECTED=true      # suppresses every order and broker read
ACT_TRADER=1           # arms the new Analyzer/Planner/Executor (logging only)
```

**`LIVE_TRADING=false` is WRONG here.** It does not mean "live trading off" — it
launches `signal_smt.py`, a *different program* with a different pipeline construction
site. The trader graft is wired into `automation/main.py` only. To run the real live
path without touching the broker, keep `LIVE_TRADING=true` and set `DISCONNECTED=true`
(`live_orders.py:25-36`, `:112`, `:135`, `:171` already honour it — no legacy code
changed for cycle 1).

Optional:

```
ACT_TRADER_BACKEND=openrouter   # or `anthropic`; default openrouter
ACT_TRADER_MODEL=              # blank -> the backend's default model
```

With `ACT_TRADER` unset the graft is never constructed: `automation/main.py`
`_build_trader` returns `None` before any import, `SessionPipeline._trader` stays
`None`, and the bar loop does zero extra work — byte-identical by construction.

## What it writes, and where

Everything lands in the live session folder (`<global>/sessions/<date>/`):

| File | Written by | Notes |
|---|---|---|
| `trader_decisions.jsonl` | Executor | intended entries, vetoes, binds, plan death |
| `thesis_state.json` | Analyzer | the standing thesis + facts-health provenance |
| `plans.json` | PlanStore | the derived plan, keyed by `plan_id` |
| `facts_snapshot.json` | Journal | the Analyzer's private fact store at 09:20 — the exact facts the thesis was decided against, for cycle-2 replay |

`facts_journal.jsonl` is **not** written in cycle 1. `agent/facts/journal.py` implements
the append-only event stream, but no cycle-1 task wires per-fact journaling into the
drivers and doing so on the bar-close path is unbudgeted I/O. Only the once-daily
snapshot is wired.

It writes **nothing else**. `events.jsonl`, `daily.json`, `hypothesis.json`,
`position.json` and `smts.json` belong to the legacy engine, which shares this
directory — the trader never opens them for writing and never calls an `smt_state`
mutator or `paths.set_state_dir()`.

## Timeline of a shadow session

| Time (ET) | What happens |
|---|---|
| 18:00 prev day | CME session opens; legacy engine runs as always |
| 09:20 | Analyzer fires **once** on its own trigger (the pipeline's 09:20 daily branch is dead — 00:00 consumes the once-per-date budget) |
| 09:20+ | On a standing thesis, the Planner derives a plan on the next bar close with 5m legs available |
| 09:20 → 09:30:30 | Settle window: state tracked, **no** intended entries |
| 09:30:30 → close | Binding, guards (max-distance 60 pts, then DOL-floor 60 pts), intended-entry records |
| any time | DOL reached / predicate falsified → plan dead, recorded once, chain goes dark |

A NEUTRAL or DOL-less thesis means the whole chain stays dark for the day. That is the
decision recorded 2026-08-17 (`l2-mechanisms.md` §2): no fallback DOL, no self-armed
thesis. Re-arming is L1's recall problem, not L2's.

## After the session

```bash
python scripts/compare_analyzer_vs_legacy.py ~/projects/auto-co-trader/global/sessions/<date>
```

Prints a time-ordered side-by-side of legacy entry intents (`new-stop-entry`,
`move-stop-entry`, `market-entry` — read-only from the legacy stream) against the
trader's intended entries, plus every veto with its reason. `--json` for the raw rows.

## Facts parity (acceptance gate 1)

```bash
python scripts/facts_parity.py --dates 2026-07-14,2026-07-15,2026-07-16,2026-07-17,2026-07-20,2026-07-21,2026-07-22,2026-07-23,2026-07-24,2026-07-27
```

Compares the online assembler against the offline `derive_facts` reference at each
09:20 boundary. Exit 0 means every date's hashed S0–S7 identity and every
`decide_thesis` contract key match. Takes ~1 minute per date (two full 17-day
`compute_facts` passes over the 1s parquets).
