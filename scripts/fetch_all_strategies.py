"""
scripts/fetch_all_strategies.py — Batch-extract strategies from git worktrees.

For each worktree, reads train.py at HEAD, extracts code above the boundary,
auto-populates a complete METADATA dict from prepare.py / results.tsv / train.py,
generates a strategy description via claude -p, and writes strategies/<name>.py.
Also updates strategies/__init__.py.

Strategy module name is derived from the branch name:
  autoresearch/financials-mar20  →  financials_mar20

Usage:
  python scripts/fetch_all_strategies.py [options]

Options:
  --force       Overwrite existing strategy files (default: skip)
  --dry-run     Show what would happen without writing any files
  --only PATH   Only process this worktree path (can repeat)
  --skip BRANCH Skip worktrees whose branch name contains BRANCH (can repeat)
  --no-registry Don't update strategies/__init__.py
  --no-llm      Skip description generation (leaves description as empty string)
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

BOUNDARY = "DO NOT EDIT BELOW THIS LINE"
STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"
INIT_PATH = STRATEGIES_DIR / "__init__.py"

BRANCH_PREFIXES = ("autoresearch/", "research/", "feature/", "exp/")

# Maps branch name substrings to sector labels (checked in order)
SECTOR_MAP = [
    ("semis",       "semiconductors"),
    ("financial",   "financials"),
    ("utilit",      "utilities"),
    ("energy",      "energy/materials"),
    ("material",    "energy/materials"),
    ("tech",        "technology"),
    ("health",      "healthcare"),
    ("consumer",    "consumer"),
    ("real",        "real estate"),
]


# ── Worktree discovery ─────────────────────────────────────────────────────────


def get_worktrees() -> list[dict]:
    """Return list of {path, commit, branch} for all worktrees that have a branch."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    worktrees: list[dict] = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):]}
        elif line.startswith("HEAD "):
            current["commit"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):]
            branch = branch.removeprefix("refs/heads/")
            current["branch"] = branch
        elif line == "" and current:
            if "branch" in current:
                worktrees.append(current)
            current = {}
    if current and "branch" in current:
        worktrees.append(current)
    return worktrees


# ── Name / key derivation ──────────────────────────────────────────────────────


def branch_to_module_name(branch: str) -> str:
    for prefix in BRANCH_PREFIXES:
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
            break
    return re.sub(r"[^a-zA-Z0-9]", "_", branch).strip("_")


def branch_to_strategy_key(branch: str) -> str:
    for prefix in BRANCH_PREFIXES:
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
            break
    return branch


# ── Sector inference ───────────────────────────────────────────────────────────


def infer_sector(branch: str) -> str:
    b = branch.lower()
    for keyword, sector in SECTOR_MAP:
        if keyword in b:
            return sector
    return "unknown"


# ── Code extraction ────────────────────────────────────────────────────────────


def extract_above_boundary(commit: str) -> list[str] | None:
    """Read train.py from git object store at commit, return lines above boundary."""
    try:
        result = subprocess.run(
            ["git", "show", f"{commit}:train.py"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return None

    lines = result.stdout.splitlines()
    boundary_idx = next(
        (i for i, line in enumerate(lines) if BOUNDARY in line), None
    )
    if boundary_idx is None:
        return None
    return lines[:boundary_idx]


# ── Metadata extraction ────────────────────────────────────────────────────────


def parse_prepare_py(worktree_path: str) -> dict:
    """
    Read TICKERS, BACKTEST_START, BACKTEST_END from prepare.py in the worktree.
    Returns empty dict if prepare.py not found or values not parseable.
    """
    prepare = Path(worktree_path) / "prepare.py"
    if not prepare.exists():
        return {}
    text = prepare.read_text(encoding="utf-8")

    out = {}

    # TICKERS = ["A", "B", ...]
    m = re.search(r'^TICKERS\s*=\s*(\[.*?\])', text, re.MULTILINE | re.DOTALL)
    if m:
        try:
            out["tickers"] = eval(m.group(1))  # safe: only list of strings
        except Exception:
            pass

    # BACKTEST_START = "YYYY-MM-DD"
    m = re.search(r'^BACKTEST_START\s*=\s*["\'](\d{4}-\d{2}-\d{2})["\']', text, re.MULTILINE)
    if m:
        out["backtest_start"] = m.group(1)

    # BACKTEST_END = "YYYY-MM-DD"
    m = re.search(r'^BACKTEST_END\s*=\s*["\'](\d{4}-\d{2}-\d{2})["\']', text, re.MULTILINE)
    if m:
        out["backtest_end"] = m.group(1)

    return out


def parse_train_py_dates(commit: str) -> dict:
    """
    Read TRAIN_END / TEST_START from train.py at commit.
    Returns empty dict if not found (older worktrees without train/test split).
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{commit}:train.py"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return {}

    text = result.stdout
    out = {}

    m = re.search(r'^TRAIN_END\s*=\s*["\'](\d{4}-\d{2}-\d{2})["\']', text, re.MULTILINE)
    if m:
        out["train_end"] = m.group(1)

    m = re.search(r'^TEST_START\s*=\s*["\'](\d{4}-\d{2}-\d{2})["\']', text, re.MULTILINE)
    if m:
        out["test_start"] = m.group(1)

    return out


def parse_results_tsv(worktree_path: str) -> dict:
    """
    Read the last 'keep' row from results.tsv in the worktree.
    Supports both old schema (sharpe, total_trades) and new schema (train_pnl, train_sharpe, etc.).
    Returns empty dict if no results.tsv or no keep rows.
    """
    tsv = Path(worktree_path) / "results.tsv"
    if not tsv.exists():
        return {}

    lines = tsv.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return {}

    headers = lines[0].split("\t")

    # Find all keep rows
    keep_rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < len(headers):
            continue
        row = dict(zip(headers, parts))
        if row.get("status", "").strip().lower() == "keep":
            keep_rows.append(row)

    if not keep_rows:
        return {}

    best = keep_rows[-1]  # last keep = most recent best
    out = {}

    if "train_sharpe" in headers:
        # New extended schema
        try:
            out["train_sharpe"] = float(best["train_sharpe"])
        except (KeyError, ValueError):
            pass
        try:
            out["train_pnl"] = float(best["train_pnl"])
        except (KeyError, ValueError):
            pass
        try:
            out["train_trades"] = int(best["total_trades"])
        except (KeyError, ValueError):
            pass
    elif "sharpe" in headers:
        # Old schema — sharpe is full-window, not train-only
        try:
            out["train_sharpe"] = float(best["sharpe"])
        except (KeyError, ValueError):
            pass
        try:
            out["train_trades"] = int(best["total_trades"])
        except (KeyError, ValueError):
            pass

    return out


# ── Description generation ─────────────────────────────────────────────────────


def generate_description(strategy_lines: list[str], strategy_key: str) -> str:
    """
    Call claude -p to generate a 2-3 sentence description of the strategy.
    Returns empty string if claude is unavailable.
    """
    code = "\n".join(strategy_lines)
    prompt = (
        f"You are looking at the strategy code for '{strategy_key}', a stock trading strategy.\n\n"
        "Write exactly 2-3 sentences describing:\n"
        "1. The entry conditions (what the screener looks for to trigger a trade)\n"
        "2. Exit / stop logic\n"
        "3. Any key parameters or filters (RSI thresholds, ATR multiples, SMA periods, etc.)\n"
        "4. What market regime this strategy suits\n\n"
        "Be specific about indicator names and their numeric thresholds. "
        "Reply with only the description text — no preamble, no bullet points, no markdown.\n\n"
        f"```python\n{code}\n```"
    )

    env_passthrough = {k: v for k, v in __import__("os").environ.items()
                       if k != "ANTHROPIC_API_KEY"}
    env_passthrough["CLAUDECODE"] = "1"

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=60,
            env=env_passthrough,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


# ── File writing ───────────────────────────────────────────────────────────────


def format_metadata_dict(meta: dict) -> list[str]:
    """Format a METADATA dict as Python source lines."""
    tickers = meta.get("tickers") or []
    ticker_repr = repr(tickers)

    def _val(v):
        if v is None:
            return "None"
        # float('nan') repr is 'nan' which is not a Python builtin — coerce to None
        if isinstance(v, float) and v != v:  # NaN check
            return "None"
        return repr(v)

    desc = meta.get("description", "")
    if len(desc) > 80:
        # Wrap long description in parentheses for readability
        desc_lines = [
            '    "description":  (',
            f'        {repr(desc)}',
            '    ),',
        ]
    else:
        desc_lines = [f'    "description":  {repr(desc)},']

    lines = [
        "METADATA = {",
        f'    "name":         {repr(meta.get("name", ""))},',
        f'    "sector":       {repr(meta.get("sector", "unknown"))},',
        f'    "tickers":      {ticker_repr},',
        f'    "train_start":  {_val(meta.get("train_start"))},',
        f'    "train_end":    {_val(meta.get("train_end"))},',
        f'    "test_start":   {_val(meta.get("test_start"))},',
        f'    "test_end":     {_val(meta.get("test_end"))},',
        f'    "source_branch": {repr(meta.get("source_branch", ""))},',
        f'    "source_commit": {repr(meta.get("source_commit", ""))},',
        f'    "train_pnl":    {_val(meta.get("train_pnl"))},',
        f'    "train_sharpe": {_val(meta.get("train_sharpe"))},',
        f'    "train_trades": {_val(meta.get("train_trades"))},',
    ] + desc_lines + ["}"]

    return lines


LEGACY_NOTE = (
    "# LEGACY_OBJECTIVE: sharpe — this strategy was optimized for Sharpe ratio (pre-Enhancement 2).\n"
    "# Before using it as the starting point for a new optimization run, see program.md §Setup step 8b."
)


def write_strategy_file(
    out_path: Path,
    module_name: str,
    branch: str,
    short_commit: str,
    meta: dict,
    strategy_lines: list[str],
) -> None:
    header = [
        '"""',
        f"strategies/{module_name}.py — Extracted from {branch} @ {short_commit}",
        '"""',
        "",
    ]
    metadata_lines = format_metadata_dict(meta)

    # Add legacy note when train_pnl is absent — indicates old Sharpe-optimizing harness
    legacy_lines = []
    if meta.get("train_pnl") is None:
        legacy_lines = ["", LEGACY_NOTE]

    content = "\n".join(header + metadata_lines + legacy_lines + [""] + strategy_lines) + "\n"
    out_path.write_text(content, encoding="utf-8")


# ── Registry update ────────────────────────────────────────────────────────────


def update_registry(module_name: str, strategy_key: str, dry_run: bool) -> None:
    """Add import and REGISTRY entry to strategies/__init__.py. Idempotent."""
    content = INIT_PATH.read_text(encoding="utf-8")
    import_line = f"from strategies import {module_name}"
    registry_entry = f'    "{strategy_key}": {module_name},'

    if import_line in content:
        print(f"  [registry] already registered: {strategy_key}")
        return

    if dry_run:
        print(f"  [registry] would add: {import_line}")
        print(f"  [registry] would add: {registry_entry}")
        return

    lines = content.splitlines()

    last_import_idx = max(
        (i for i, ln in enumerate(lines) if ln.startswith("from strategies import")),
        default=-1,
    )
    lines.insert(last_import_idx + 1, import_line)

    closing_brace_idx = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == "}"),
        None,
    )
    if closing_brace_idx is not None:
        lines.insert(closing_brace_idx, registry_entry)
    else:
        lines.append(registry_entry)

    INIT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [registry] added: {strategy_key} -> {module_name}")


# ── Main extraction ────────────────────────────────────────────────────────────


def process_worktree(
    wt: dict,
    force: bool,
    dry_run: bool,
    update_registry_flag: bool,
    use_llm: bool,
) -> bool:
    """Extract strategy from one worktree. Returns True if a file was written."""
    branch = wt["branch"]
    commit = wt["commit"]
    short_commit = commit[:7]
    module_name = branch_to_module_name(branch)
    strategy_key = branch_to_strategy_key(branch)
    out_path = STRATEGIES_DIR / f"{module_name}.py"

    print(f"\n{'-'*60}")
    print(f"Branch:   {branch}")
    print(f"Commit:   {short_commit}")
    print(f"Module:   {module_name}  (key: {strategy_key})")
    print(f"Output:   strategies/{module_name}.py")

    if out_path.exists() and not force:
        print(f"  [skip] file already exists (use --force to overwrite)")
        return False

    strategy_lines = extract_above_boundary(commit)
    if strategy_lines is None:
        print(f"  [skip] train.py not found or boundary missing at {short_commit}")
        return False

    # --- Gather metadata from worktree sources ---
    prep = parse_prepare_py(wt["path"])
    train_dates = parse_train_py_dates(commit)
    metrics = parse_results_tsv(wt["path"])

    # Resolve train/test window
    train_start = prep.get("backtest_start")
    test_end = prep.get("backtest_end")
    train_end = train_dates.get("train_end") or test_end   # fallback: no split
    test_start = train_dates.get("test_start") or test_end  # fallback: no split

    # Build description
    description = ""
    if use_llm and not dry_run:
        print(f"  [llm] generating description...")
        description = generate_description(strategy_lines, strategy_key)
        if description:
            print(f"  [llm] done ({len(description)} chars)")
        else:
            print(f"  [llm] skipped (claude not available)")
    elif dry_run and use_llm:
        print(f"  [llm] would generate description via claude -p")

    meta = {
        "name":          strategy_key,
        "sector":        infer_sector(branch),
        "tickers":       prep.get("tickers", []),
        "train_start":   train_start,
        "train_end":     train_end,
        "test_start":    test_start,
        "test_end":      test_end,
        "source_branch": branch,
        "source_commit": short_commit,
        "train_pnl":     metrics.get("train_pnl"),
        "train_sharpe":  metrics.get("train_sharpe"),
        "train_trades":  metrics.get("train_trades"),
        "description":   description,
    }

    # Print metadata summary
    print(f"  sector:       {meta['sector']}")
    print(f"  tickers:      {len(meta['tickers'])} tickers")
    print(f"  window:       {train_start} .. {train_end} | test {test_start} .. {test_end}")
    sharpe_str = f"{meta['train_sharpe']:.3f}" if meta['train_sharpe'] is not None else "n/a"
    pnl_str = f"{meta['train_pnl']:.2f}" if meta['train_pnl'] is not None else "n/a"
    trades_str = str(meta['train_trades']) if meta['train_trades'] is not None else "n/a"
    print(f"  metrics:      sharpe={sharpe_str}  pnl={pnl_str}  trades={trades_str}")

    if dry_run:
        print(f"  [dry-run] would write {len(strategy_lines)} strategy lines to {out_path.name}")
    else:
        write_strategy_file(out_path, module_name, branch, short_commit, meta, strategy_lines)
        print(f"  [wrote] {out_path}")

    if update_registry_flag:
        update_registry(module_name, strategy_key, dry_run)

    return True


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-extract strategies from git worktrees into strategies/<name>.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without writing")
    parser.add_argument(
        "--only", action="append", metavar="PATH", default=[],
        help="Only process this worktree path (repeat for multiple)",
    )
    parser.add_argument(
        "--skip", action="append", metavar="BRANCH", default=[],
        help="Skip worktrees whose branch contains this string",
    )
    parser.add_argument("--no-registry", action="store_true", help="Don't update __init__.py")
    parser.add_argument("--no-llm", action="store_true", help="Skip description generation")
    args = parser.parse_args()

    worktrees = get_worktrees()

    if args.only:
        only_resolved = {str(Path(p).resolve()) for p in args.only}
        worktrees = [w for w in worktrees if str(Path(w["path"]).resolve()) in only_resolved]

    if args.skip:
        worktrees = [w for w in worktrees if not any(s in w["branch"] for s in args.skip)]

    if not worktrees:
        print("No worktrees to process.")
        sys.exit(0)

    label = "  [DRY RUN]" if args.dry_run else ""
    print(f"Processing {len(worktrees)} worktree(s){label}:")

    written = 0
    for wt in worktrees:
        ok = process_worktree(
            wt,
            force=args.force,
            dry_run=args.dry_run,
            update_registry_flag=not args.no_registry,
            use_llm=not args.no_llm,
        )
        if ok:
            written += 1

    print(f"\n{'-'*60}")
    print(f"Done. {'Would write' if args.dry_run else 'Wrote'} {written}/{len(worktrees)} strategy file(s).")
    if written > 0 and not args.dry_run:
        print("\nRun tests: python -m pytest tests/test_registry.py -q")


if __name__ == "__main__":
    main()
