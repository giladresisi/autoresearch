# Feature: Phase 1 — Infrastructure Setup

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Update `pyproject.toml` to swap out the GPU/NLP dependency stack (torch, kernels, rustbpe, tiktoken,
requests) and replace it with the data-science stack needed for stock backtesting (yfinance, pandas,
numpy, pyarrow — most already present). Run `uv sync` to reconcile the lock file. Validate that all
required packages import cleanly and that yfinance can reach the Yahoo Finance API.

This is a pure infrastructure change — no application logic is written. It unblocks Phase 2 (prepare.py)
and Phase 3 (train.py) by guaranteeing a clean, reproducible environment.

## User Story

As a developer setting up the autoresearch stock strategy optimizer,
I want a clean Python environment with only the required dependencies installed,
So that subsequent phases can import yfinance, pandas, numpy, and pyarrow without conflicts or
unnecessary GPU packages bloating the environment.

## Problem Statement

The current `pyproject.toml` references a CUDA-based PyTorch index and several NLP packages
(`torch==2.9.1`, `kernels`, `rustbpe`, `tiktoken`) plus `requests`, none of which are needed for
the stock backtesting pipeline. These packages:
- Pull in large binary wheels (torch alone is ~2 GB with CUDA)
- Slow down `uv sync` significantly
- Create unnecessary failure points on machines without CUDA drivers
- Obscure the project's actual dependency surface

Additionally, `yfinance` (the only new runtime dependency) is missing entirely.

## Solution Statement

Edit `pyproject.toml` to:
1. Remove `torch`, `kernels`, `rustbpe`, `tiktoken`, `requests` from `dependencies`
2. Remove the `[tool.uv.sources]` block (torch index override)
3. Remove the `[[tool.uv.index]]` entry for `pytorch-cu128`
4. Add `yfinance` (unpinned, latest stable)

Then run `uv sync` to regenerate `uv.lock` against the new dependency set.

## Feature Metadata

**Feature Type**: Refactor (dependency cleanup) + Enhancement (add yfinance)
**Complexity**: Low
**Primary Systems Affected**: `pyproject.toml`, `uv.lock`
**Dependencies**: yfinance (new), pandas (existing), numpy (existing), pyarrow (existing), matplotlib (existing)
**Breaking Changes**: No application code is changed. `train.py` and `prepare.py` currently import
`torch` — this will break their imports, but both files are scheduled for full rewrites in Phases 2–3
and are not expected to run during Phase 1.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `pyproject.toml` (lines 1–28) — Why: The exact file to be modified; all current deps and index blocks
  must be understood before editing

### New Files to Create

None. This phase only modifies existing files.

### Files Modified

- `pyproject.toml` — Remove GPU/NLP deps + torch index; add yfinance
- `uv.lock` — Regenerated automatically by `uv sync` (do not hand-edit)

### Relevant Documentation — READ BEFORE IMPLEMENTING

- [yfinance PyPI page](https://pypi.org/project/yfinance/) — latest version, install name
- [uv dependency management docs](https://docs.astral.sh/uv/concepts/dependencies/) — syntax for adding
  unpinned deps, removing index overrides

### External API Research

**No API keys required.** yfinance accesses Yahoo Finance's public endpoints without authentication.
The smoke test in Task 3.2 makes a live HTTP call to verify connectivity — this requires internet access
but no credentials.

### Patterns to Follow

**Dependency format in pyproject.toml**: `"package>=X.Y.Z"` with lower-bound pins only (see existing
`numpy>=2.2.6`, `pandas>=2.3.3`, `pyarrow>=21.0.0`). For yfinance, add as `"yfinance"` (no pin) since
it is not yet integrated and we want the latest stable release. The pin can be tightened in Phase 2
after verifying a specific version works.

**uv.lock**: Never hand-edit. Always regenerate via `uv sync`.

---

## CURRENT STATE OF pyproject.toml

```toml
[project]
name = "autoresearch"
version = "0.1.0"
description = "Autonomous pretraining research swarm"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "kernels>=0.11.7",        # ← REMOVE
    "matplotlib>=3.10.8",     # keep
    "numpy>=2.2.6",           # keep
    "pandas>=2.3.3",          # keep
    "pyarrow>=21.0.0",        # keep
    "requests>=2.32.0",       # ← REMOVE (yfinance brings its own HTTP layer)
    "rustbpe>=0.1.0",         # ← REMOVE
    "tiktoken>=0.11.0",       # ← REMOVE
    "torch==2.9.1",           # ← REMOVE
]

[tool.uv.sources]
torch = [
    { index = "pytorch-cu128" },   # ← REMOVE entire block
]

[[tool.uv.index]]
name = "pytorch-cu128"
url = "https://download.pytorch.org/whl/cu128"
explicit = true                    # ← REMOVE entire block
```

## TARGET STATE OF pyproject.toml

```toml
[project]
name = "autoresearch"
version = "0.1.0"
description = "Autonomous pretraining research swarm"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "matplotlib>=3.10.8",
    "numpy>=2.2.6",
    "pandas>=2.3.3",
    "pyarrow>=21.0.0",
    "yfinance",
]
```

No `[tool.uv.sources]` block. No `[[tool.uv.index]]` block.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────┐
│ WAVE 1: Edit pyproject.toml              │
├──────────────────────────────────────────┤
│ Task 1.1: UPDATE pyproject.toml          │
│ Agent: implementer                       │
└──────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────┐
│ WAVE 2: Sync environment                 │
├──────────────────────────────────────────┤
│ Task 2.1: RUN uv sync                    │
│ Agent: implementer                       │
└──────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────┐
│ WAVE 3: Validate (Parallel)              │
├──────────────────────────────────────────┤
│ Task 3.1: VERIFY imports │ Task 3.2:    │
│ Agent: validator         │ VERIFY net   │
│                          │ Agent: valid │
└──────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Single task**: Edit pyproject.toml
**Wave 2 — Single task**: `uv sync` (must follow Wave 1)
**Wave 3 — Fully parallel**: Import validation + yfinance connectivity check (both depend only on Wave 2)

### Interface Contracts

**Wave 1 → Wave 2**: `pyproject.toml` must be valid TOML with no torch/kernels/rustbpe/tiktoken/requests
entries before `uv sync` runs.

**Wave 2 → Wave 3**: `uv.lock` must reflect the new deps; `uv run python` must resolve to an environment
containing yfinance, pandas, numpy, pyarrow. Both Wave 3 tasks consume this environment independently.

### Synchronization Checkpoints

**After Wave 1**: `python -c "import tomllib; tomllib.loads(open('pyproject.toml').read()); print('TOML valid')"` (Python 3.11+ built-in) or `uv run python -c "import tomllib; tomllib.loads(open('pyproject.toml').read())"` — must print without error.
**After Wave 2**: `uv sync` must exit 0 with no error output.
**After Wave 3**: Both validation commands must pass.

---

## IMPLEMENTATION PLAN

### Phase 1: Edit pyproject.toml

Edit `pyproject.toml` to match the target state defined above. The changes are:

1. Remove `"kernels>=0.11.7"` from dependencies
2. Remove `"requests>=2.32.0"` from dependencies
3. Remove `"rustbpe>=0.1.0"` from dependencies
4. Remove `"tiktoken>=0.11.0"` from dependencies
5. Remove `"torch==2.9.1"` from dependencies
6. Add `"yfinance"` to dependencies (alphabetically: after `pyarrow`, before end of list)
7. Remove the entire `[tool.uv.sources]` section (3 lines)
8. Remove the entire `[[tool.uv.index]]` section (4 lines)

### Phase 2: Sync Environment

Run `uv sync` from the project root. This will:
- Resolve the new dependency graph (much faster without torch)
- Download yfinance and any new transitive deps
- Regenerate `uv.lock`

### Phase 3: Validate

Two checks in parallel:

**3.1 — Import validation**: Confirm all 4 required runtime packages import correctly.
**3.2 — yfinance connectivity**: Download 5 days of hourly AAPL data and confirm the result is a
non-empty DataFrame.

---

## STEP-BY-STEP TASKS

### WAVE 1: Edit Configuration

#### Task 1.1: UPDATE pyproject.toml

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: A valid `pyproject.toml` with GPU/NLP deps removed and `yfinance` added
- **IMPLEMENT**:
  Replace the entire `pyproject.toml` content with the target state shown in the CONTEXT REFERENCES
  section above. The file is small (28 lines); a full rewrite is safer than surgical edits to avoid
  leaving stale whitespace or orphaned table headers.

  Final file should contain exactly:
  ```toml
  [project]
  name = "autoresearch"
  version = "0.1.0"
  description = "Autonomous pretraining research swarm"
  readme = "README.md"
  requires-python = ">=3.10"
  dependencies = [
      "matplotlib>=3.10.8",
      "numpy>=2.2.6",
      "pandas>=2.3.3",
      "pyarrow>=21.0.0",
      "yfinance",
  ]
  ```

  No `[tool.uv.sources]` block. No `[[tool.uv.index]]` block. No trailing blank lines beyond a
  single newline at end of file.

- **VALIDATE**:
  ```bash
  # Confirm removed packages are gone
  grep -E "torch|kernels|rustbpe|tiktoken|requests|pytorch" pyproject.toml && echo "FAIL: old deps remain" || echo "PASS: old deps removed"
  # Confirm yfinance is present
  grep "yfinance" pyproject.toml && echo "PASS: yfinance present" || echo "FAIL: yfinance missing"
  # Confirm TOML is syntactically valid
  uv run python -c "import tomllib; tomllib.loads(open('pyproject.toml').read()); print('PASS: TOML valid')"
  ```

**Wave 1 Checkpoint**:
```bash
grep -c "yfinance" pyproject.toml  # must output 1
grep -c "torch" pyproject.toml     # must output 0
```

---

### WAVE 2: Sync Environment

#### Task 2.1: RUN uv sync

- **WAVE**: 2
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1, 3.2]
- **PROVIDES**: Reconciled `uv.lock` and installed virtual environment with yfinance available
- **USES_FROM_WAVE_1**: Task 1.1 provides valid `pyproject.toml` without torch/GPU index references
- **IMPLEMENT**:
  ```bash
  uv sync
  ```
  This command will:
  - Read the updated `pyproject.toml`
  - Remove torch, kernels, rustbpe, tiktoken, requests from the lock
  - Resolve and install yfinance + transitive deps (httpx, multitasking, peewee, etc.)
  - Update `uv.lock`

  Expected behavior: `uv sync` will run without errors. Because torch is ~2 GB and is being removed,
  the sync should be significantly faster than the original install (yfinance + deps is ~50 MB total).

  **If uv sync fails** (common failure modes and fixes):
  - `error: No solution found`: A transitive dep conflict. Run `uv sync --verbose` to identify the
    conflict. If yfinance conflicts with pandas/numpy pins, relax the conflicting pin.
  - `error: Failed to download`: Network issue. Retry. If persistent, try `uv sync --offline` to
    verify it's not a network issue (expected to fail if yfinance was never cached).
  - `TOML parse error`: Wave 1 produced invalid TOML. Re-check `pyproject.toml` against target state.

- **VALIDATE**:
  ```bash
  # Exit code 0 means success — uv sync itself is the validation
  uv sync && echo "PASS: uv sync succeeded" || echo "FAIL: uv sync failed"
  # Verify yfinance is in the resolved environment
  uv run python -c "import yfinance; print('PASS: yfinance importable, version:', yfinance.__version__)"
  ```

**Wave 2 Checkpoint**:
```bash
uv run python -c "import yfinance; print(yfinance.__version__)"
# Must print a version string (e.g. "0.2.x") without ImportError
```

---

### WAVE 3: Validation (Run in parallel)

#### Task 3.1: VERIFY all required imports

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: Confirmed that all 4 runtime packages (yfinance, pandas, numpy, pyarrow) import cleanly
- **USES_FROM_WAVE_2**: Task 2.1 provides the synced environment
- **IMPLEMENT**:
  Run the combined import check:
  ```bash
  uv run python -c "
  import yfinance
  import pandas
  import numpy
  import pyarrow
  print('yfinance:', yfinance.__version__)
  print('pandas:', pandas.__version__)
  print('numpy:', numpy.__version__)
  print('pyarrow:', pyarrow.__version__)
  print('PASS: all imports OK')
  "
  ```
  All four lines must print without `ImportError` or `ModuleNotFoundError`.

  **Also verify removed packages are NOT importable** (confirming clean removal):
  ```bash
  uv run python -c "
  packages_removed = ['torch', 'tiktoken', 'rustbpe']
  for pkg in packages_removed:
      try:
          __import__(pkg)
          print(f'WARNING: {pkg} still importable (may be system-installed, not a uv env issue)')
      except ImportError:
          print(f'PASS: {pkg} not available in uv env')
  "
  ```
  Note: If any removed package prints "WARNING", it means it's installed system-wide but not in the uv
  venv. This is acceptable — the uv environment is isolated.

- **VALIDATE**:
  All lines must print without error. The final line must be `PASS: all imports OK`.

#### Task 3.2: VERIFY yfinance network connectivity

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: []
- **PROVIDES**: Confirmed yfinance can reach Yahoo Finance and return valid OHLCV data
- **USES_FROM_WAVE_2**: Task 2.1 provides the synced environment with yfinance installed
- **IMPLEMENT**:
  Run the connectivity smoke test:
  ```bash
  uv run python -c "
  import yfinance as yf
  import pandas as pd

  print('Downloading 5 days of AAPL hourly data...')
  ticker = yf.Ticker('AAPL')
  df = ticker.history(period='5d', interval='1h', auto_adjust=True, prepost=False)

  assert isinstance(df, pd.DataFrame), f'Expected DataFrame, got {type(df)}'
  assert len(df) > 0, f'DataFrame is empty — download may have failed'
  assert 'Open' in df.columns, f'Missing Open column. Columns: {df.columns.tolist()}'
  assert 'High' in df.columns, f'Missing High column'
  assert 'Low' in df.columns, f'Missing Low column'
  assert 'Close' in df.columns, f'Missing Close column'
  assert 'Volume' in df.columns, f'Missing Volume column'

  print(f'PASS: downloaded {len(df)} rows')
  print(f'Columns: {df.columns.tolist()}')
  print(df.tail(3).to_string())
  "
  ```

  **If this fails** (common failure modes):
  - `Empty DataFrame`: Yahoo Finance occasionally blocks requests. Wait 60 seconds and retry.
  - `JSONDecodeError` / `HTTPError`: yfinance API instability. Retry up to 3 times with a 30-second
    delay. If still failing, document the failure and note it as a transient external issue — it does
    not block Phase 2 development (prepare.py can still be written and tested later).
  - `AssertionError: Missing column`: yfinance may have changed its column naming. Check actual
    `df.columns` and update the PRD's column mapping if needed.

- **VALIDATE**:
  Output must include `PASS: downloaded N rows` where N > 0, and show the expected column names.

**Wave 3 Checkpoint**:
```bash
# Both Task 3.1 and 3.2 must pass before Phase 1 is complete
echo "Wave 3 complete — both validation tasks must show PASS"
```

---

## TESTING STRATEGY

This phase has no application logic — there are no functions, classes, or business rules to unit test.
All verification is done via CLI validation commands. No pytest files are created.

**Justification for no automated test files**: The deliverable is a correctly edited `pyproject.toml`
and a synchronized environment. The "tests" are the import checks and network smoke test run directly
as validation commands. Creating a pytest suite for `import yfinance` would be redundant and
violate the "don't over-engineer" principle.

### Validation Tests (all automated via CLI)

| # | What | Automation | Tool | Command |
|---|------|-----------|------|---------|
| V1 | Old deps absent from pyproject.toml | ✅ | bash grep | `grep -c "torch" pyproject.toml` → 0 |
| V2 | yfinance present in pyproject.toml | ✅ | bash grep | `grep -c "yfinance" pyproject.toml` → 1 |
| V3 | TOML syntax valid | ✅ | python tomllib | `uv run python -c "import tomllib; ..."` |
| V4 | uv sync succeeds | ✅ | uv | `uv sync && echo "PASS"` |
| V5 | All 4 packages import | ✅ | python | `uv run python -c "import yfinance, pandas, numpy, pyarrow"` |
| V6 | yfinance API connectivity | ✅ | python + yfinance | `uv run python -c "... ticker.history(...)"` |

### Manual Tests

None. All validation is automatable via CLI.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ CLI validation (bash/python) | 6 | 100% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 6 | 100% |

**Goal**: All 6 validation commands exit 0 and print PASS.

---

## VALIDATION COMMANDS

### Level 1: Configuration Correctness

```bash
# V1: Confirm all removed packages are gone
grep -E "torch|kernels|rustbpe|tiktoken|requests|pytorch-cu128" pyproject.toml \
  && echo "FAIL: stale entries found" || echo "PASS: no stale entries"

# V2: Confirm yfinance is present
grep "yfinance" pyproject.toml && echo "PASS: yfinance present" || echo "FAIL: yfinance missing"

# V3: TOML syntax check
uv run python -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print('PASS: TOML valid')
print('deps:', data['project']['dependencies'])
"
```

### Level 2: Environment Sync

```bash
# V4: Run uv sync
uv sync && echo "PASS: uv sync succeeded"
```

### Level 3: Import Validation

```bash
# V5: All required packages importable
uv run python -c "
import yfinance, pandas, numpy, pyarrow
print('PASS: yfinance', yfinance.__version__)
print('PASS: pandas', pandas.__version__)
print('PASS: numpy', numpy.__version__)
print('PASS: pyarrow', pyarrow.__version__)
"
```

### Level 4: Network Connectivity

```bash
# V6: yfinance smoke test
uv run python -c "
import yfinance as yf
df = yf.Ticker('AAPL').history(period='5d', interval='1h', auto_adjust=True, prepost=False)
assert len(df) > 0, 'Empty DataFrame'
assert all(c in df.columns for c in ['Open','High','Low','Close','Volume']), f'Bad columns: {df.columns.tolist()}'
print(f'PASS: {len(df)} rows downloaded')
print(df.tail(2)[['Open','High','Low','Close','Volume']].to_string())
"
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `pyproject.toml` `dependencies` list contains exactly: `matplotlib`, `numpy`, `pandas`, `pyarrow`, `yfinance` — no other packages
- [ ] `pyproject.toml` has no `[tool.uv.sources]` block
- [ ] `pyproject.toml` has no `[[tool.uv.index]]` block (pytorch-cu128 index removed)
- [ ] `uv.lock` is regenerated to reflect the new dependency set (no torch/CUDA entries)

### Integration / E2E
- [ ] `uv sync` completes with exit code 0 — verified by: `uv sync && echo "PASS"`
- [ ] All 4 runtime packages import without error — verified by: `uv run python -c "import yfinance, pandas, numpy, pyarrow; print('OK')"`
- [ ] `yfinance.__version__` is printable (package metadata intact) — verified by: `uv run python -c "import yfinance; print(yfinance.__version__)"`
- [ ] `yf.Ticker('AAPL').history(period='5d', interval='1h')` returns a DataFrame with > 0 rows and columns `Open`, `High`, `Low`, `Close`, `Volume` — verified by: smoke test command in Task 3.2

### Error Handling
- [ ] If the yfinance smoke test returns an empty DataFrame, validation fails visibly (assertion error, not silent pass)

### Non-Functional
- [ ] No application code (`train.py`, `prepare.py`, or any other `.py` file) is modified

### Out of Scope
- Rewriting `prepare.py` — Phase 2
- Rewriting `train.py` — Phase 3
- Verifying `train.py` or `prepare.py` still run (they will break; this is expected)
- Pinning yfinance to a specific version — deferred to Phase 2

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: `pyproject.toml` updated (old deps removed, yfinance added, torch index blocks removed)
- [ ] Task 2.1: `uv sync` ran successfully, `uv.lock` regenerated
- [ ] Task 3.1: All 4 import checks passed
- [ ] Task 3.2: yfinance connectivity smoke test passed
- [ ] Level 1 validation: all grep checks pass
- [ ] Level 2 validation: `uv sync` exit 0
- [ ] Level 3 validation: `uv run python -c "import yfinance, pandas, numpy, pyarrow"` exits 0
- [ ] Level 4 validation: AAPL smoke test returns > 0 rows with OHLCV columns
- [ ] All acceptance criteria checked
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Why remove `requests`?

`requests` was used in the original `autoresearch` for downloading pretraining data shards. yfinance
manages its own HTTP layer (via `httpx` / `requests` transitively — it will still be available as a
transitive dep of yfinance). Explicitly pinning `requests` as a direct dep adds no value and clutters
the dependency list.

### Why not pin yfinance to a specific version?

yfinance is an unofficial Yahoo Finance scraper. The API surface changes frequently. Pinning to a
specific version could lock in a broken version if Yahoo changes their endpoints. For Phase 1, we
install latest and note the version in the validation output. Phase 2 can tighten the pin after
verifying the exact version works for the full data pipeline.

### uv.lock will change significantly

Removing torch will drop hundreds of transitive deps from `uv.lock` (CUDA kernels, etc.). The lock
file diff will be large. This is expected and correct.

### This phase does NOT run prepare.py or train.py

Both files currently import `torch` (original code) and will fail after this change. This is
acceptable — both files are scheduled for full rewrites in Phases 2 and 3 respectively.

### Potential conflict: numpy/pandas version pins

`yfinance` typically requires `pandas>=1.x` and `numpy>=1.x`. Our existing pins (`numpy>=2.2.6`,
`pandas>=2.3.3`) are both newer than yfinance's minimums. No conflict expected.
If `uv sync` reports a conflict, check `uv sync --verbose` and relax the lower bound on the
conflicting package (e.g., `numpy>=1.26` instead of `numpy>=2.2.6`) as a last resort.

### Windows note

Running on Windows 11 with uv 0.9.26. `uv sync` on Windows creates the venv in `.venv/` in the
project root. All `uv run python -c "..."` commands use this venv automatically.
