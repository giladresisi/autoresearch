"""
scripts/extract_strategy.py — Extract strategy code from a worktree commit.

Reads train.py from a git tag or commit hash, extracts all code above the
# DO NOT EDIT BELOW THIS LINE boundary, and writes it to strategies/<name>.py.

This is the only sanctioned path for adding strategy code to master.
After extraction, manually add a METADATA dict and register the module
in strategies/__init__.py.

Usage:
  python scripts/extract_strategy.py --tag <git-tag-or-commit> --name <strategy-name>

Example:
  python scripts/extract_strategy.py --tag e9886df --name energy_momentum_v1
  => Writes strategies/energy_momentum_v1.py
"""
import argparse
import subprocess
import sys
from pathlib import Path

# The boundary string that separates extractable strategy code from the evaluation harness.
# Uses the core phrase as a substring so it matches regardless of surrounding decorative
# characters (e.g. "# ── DO NOT EDIT BELOW THIS LINE ───────────────────────────────────").
BOUNDARY = "DO NOT EDIT BELOW THIS LINE"

STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"


def extract(tag: str, name: str) -> None:
    """Read train.py from tag, extract above the DO NOT EDIT boundary, write to strategies/<name>.py."""
    try:
        result = subprocess.run(
            ["git", "show", f"{tag}:train.py"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: git show {tag}:train.py failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)

    lines = result.stdout.splitlines()

    # Find where the harness begins — everything above is extractable strategy code
    boundary_idx = next(
        (i for i, line in enumerate(lines) if BOUNDARY in line),
        None,
    )
    if boundary_idx is None:
        print(
            f"ERROR: boundary '{BOUNDARY}' not found in {tag}:train.py\n"
            "The train.py at this commit may not be compatible with extraction.",
            file=sys.stderr,
        )
        sys.exit(1)

    extractable = lines[:boundary_idx]

    # Prepend a generated module docstring so the file is self-documenting
    header = [
        '"""',
        f"strategies/{name}.py — Extracted from {tag}:train.py",
        "",
        "TODO: Replace this docstring with a description of the strategy.",
        "TODO: Add a METADATA dict (see prd.md §6a for required fields).",
        "TODO: Register this module in strategies/__init__.py.",
        '"""',
        "",
    ]

    out_path = STRATEGIES_DIR / f"{name}.py"
    out_path.write_text("\n".join(header + extractable) + "\n", encoding="utf-8")

    print(f"Extracted {tag}:train.py => {out_path}")
    print()
    print("Next steps:")
    print(f"  1. Add a METADATA dict to strategies/{name}.py (see prd.md §6a)")
    print(f"  2. Add to strategies/__init__.py:")
    print(f"       from strategies import {name}")
    print(f"       REGISTRY['<strategy-name>'] = {name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract strategy code from a worktree git commit into strategies/<name>.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="Git tag or commit hash (e.g. e9886df or energy-momentum-v1)",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Output module name without .py (e.g. energy_momentum_v1)",
    )
    args = parser.parse_args()
    extract(args.tag, args.name)


if __name__ == "__main__":
    main()
