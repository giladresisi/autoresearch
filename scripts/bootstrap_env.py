#!/usr/bin/env python
"""Bootstrap the uv environment for the machine you're on.

Some machines block the UNSIGNED `_ssl.pyd` / libssl that ships with uv's *managed*
CPython, so `import ssl` fails under the managed interpreter and the project can't run.
The known trigger is Windows **Smart App Control** (enforces Authenticode signing on
loaded DLLs); the managed build is unsigned, the python.org build is PSF-signed.

This script is idempotent and safe to run on ANY machine:

  * If `uv run python -c "import ssl"` already works  -> NO-OP (the common case:
    Linux, macOS, Windows without Smart App Control). Nothing is changed.
  * If it's blocked -> pin uv to a PSF-signed *system* Python via the user-level uv
    config (`%APPDATA%\\uv\\uv.toml` on Windows, `~/.config/uv/uv.toml` elsewhere) and
    rebuild the project `.venv` from a signed system Python 3.10. If no signed 3.10 is
    found, it prints the one manual step (install python.org 3.10) and exits non-zero.

The user-level config is machine-scoped on purpose: the block is a property of the
machine, not the project, so it must NOT live in the repo (it would break clones on
machines that don't have the block). This script is the portable part — it travels with
the repo and applies the fix only where it's actually needed.

Run it with a Python that has working ssl (e.g. the system Python), from anywhere:
    python scripts/bootstrap_env.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_PY_ORG_URL = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command from the repo root, capturing output (never raises on non-zero)."""
    return subprocess.run(
        cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, **kw
    )


def uv_ssl_ok() -> tuple[bool, str]:
    """True iff `uv run python -c "import ssl"` succeeds. Returns (ok, detail)."""
    try:
        cp = _run(["uv", "run", "--no-sync", "python", "-c", "import ssl"])
    except FileNotFoundError:
        return False, "uv not found on PATH — install uv first (https://docs.astral.sh/uv/)."
    detail = (cp.stderr or cp.stdout or "").strip()
    return cp.returncode == 0, detail


def find_signed_python310() -> str | None:
    """Locate a system Python 3.10 whose ssl actually imports (i.e. signed / allowed)."""
    candidates: list[list[str]] = []
    if os.name == "nt":
        candidates.append(["py", "-3.10"])
        for p in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python310/python.exe",
            Path("C:/Program Files/Python310/python.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python310-32/python.exe",
        ):
            if p.is_file():
                candidates.append([str(p)])
    else:
        for name in ("python3.10", "python3"):
            from shutil import which
            w = which(name)
            if w:
                candidates.append([w])

    probe = "import ssl, sys; print(sys.version_info[0], sys.version_info[1], sys.executable)"
    for cand in candidates:
        cp = _run([*cand, "-c", probe])
        if cp.returncode == 0:
            out = (cp.stdout or "").split()
            if len(out) >= 3 and out[0] == "3" and out[1] == "10":
                return out[2]  # real interpreter path (resolves `py -3.10`)
    return None


def user_config_path() -> Path:
    """User-level uv config path for this OS."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return base / "uv" / "uv.toml"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "uv" / "uv.toml"


_CONFIG_BODY = (
    "# Machine-level uv config (THIS machine only; not tracked in any git repo).\n"
    "# uv's managed CPython is blocked here (unsigned _ssl.pyd / libssl), so use the\n"
    "# PSF-signed python.org system interpreter instead. Written by scripts/bootstrap_env.py.\n"
    'python-preference = "only-system"\n'
    'python-downloads = "never"\n'
)


def write_user_config() -> Path:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and "only-system" in path.read_text(encoding="utf-8"):
        print(f"  user-level uv config already set: {path}")
        return path
    path.write_text(_CONFIG_BODY, encoding="utf-8")
    print(f"  wrote user-level uv config: {path}")
    return path


def rebuild_venv(py: str) -> bool:
    print(f"  rebuilding .venv from signed Python: {py}")
    cp = _run(["uv", "venv", "--python", py, ".venv"])
    if cp.returncode != 0:
        print(cp.stderr or cp.stdout)
        return False
    cp = _run(["uv", "sync"])
    if cp.returncode != 0:
        print(cp.stderr or cp.stdout)
        return False
    return True


def main() -> int:
    ok, detail = uv_ssl_ok()
    if ok:
        print("Environment OK — uv can import ssl. No changes needed.")
        return 0

    print("uv's interpreter cannot import ssl — applying the signed-Python workaround.")
    if detail:
        print(f"  detail: {detail.splitlines()[-1]}")

    write_user_config()
    py = find_signed_python310()
    if py is None:
        print(
            "\nNo signed system Python 3.10 found. Install one, then re-run this script:\n"
            f"  Windows: download {_PY_ORG_URL}\n"
            "           and run it (per-user is fine), then: python scripts/bootstrap_env.py\n"
            "  Linux/macOS: install python3.10 from your package manager or python.org\n"
        )
        return 1

    if not rebuild_venv(py):
        print("\nFailed to rebuild .venv — see output above.")
        return 1

    ok, detail = uv_ssl_ok()
    if ok:
        print("Done — uv now uses the signed Python and can import ssl.")
        return 0
    print(f"\nStill blocked after rebuild. Last detail: {detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
