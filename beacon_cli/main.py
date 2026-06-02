"""Beacon CLI entry-point — cross-platform dispatcher.

Resolves the install layout (Homebrew / pipx / editable / source checkout)
and routes the command either to the legacy `bin/beacon` bash script
(when bash is available) or to a Python-native fallback (Windows + minimal
commands like `--version` / `help`).

This is intentionally minimal scaffolding. The full bash-to-Python
rewrite lives behind ms-44 e-695.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ._version import __version__


# ---------------------------------------------------------------------------
# Layout resolution
# ---------------------------------------------------------------------------


def _find_repo_root() -> Optional[Path]:
    """Locate the Beacon source tree (containing `lib/commands.py`).

    Search order:
      1. $BEACON_DIR if set
      2. Two parents up from this file (source / editable install)
      3. Sibling `_bundled_lib/` next to this package (wheel install)
      4. None — we couldn't find it; caller must fall back to bundled stubs

    When the wheel-bundled layout is detected, the returned path is a synthetic
    root whose `lib/` is actually `beacon_cli/_bundled_lib/`. The bash script
    is not bundled in that case (bash users get a clearer error).
    """
    env_root = os.environ.get("BEACON_DIR")
    if env_root:
        p = Path(env_root)
        if (p / "lib" / "commands.py").exists():
            return p

    # editable / source: beacon_cli/main.py -> beacon_cli/ -> repo root
    here = Path(__file__).resolve().parent.parent
    if (here / "lib" / "commands.py").exists():
        return here

    # wheel install: lib/ has been remapped to beacon_cli/_bundled_lib/
    bundled = Path(__file__).resolve().parent / "_bundled_lib"
    if (bundled / "commands.py").exists():
        # Return the package directory; commands_py path is resolved separately
        # below via _resolve_commands_py(root).
        return Path(__file__).resolve().parent

    return None


def _resolve_commands_py(root: Path) -> Optional[Path]:
    """Find commands.py inside either the source layout or the bundled wheel."""
    candidate = root / "lib" / "commands.py"
    if candidate.exists():
        return candidate
    candidate = root / "_bundled_lib" / "commands.py"
    if candidate.exists():
        return candidate
    return None


def _resolve_lib_dir(root: Path) -> Optional[Path]:
    """Return the directory that should be added to sys.path so that
    `import store`, `import core`, etc. resolve correctly."""
    if (root / "lib" / "commands.py").exists():
        return root / "lib"
    if (root / "_bundled_lib" / "commands.py").exists():
        return root / "_bundled_lib"
    return None


def _find_bash() -> Optional[str]:
    """Return path to a usable bash binary, or None on Windows-native."""
    return shutil.which("bash")


def _find_bin_beacon(root: Path) -> Optional[Path]:
    """Return path to the bash `beacon` script if present."""
    candidate = root / "bin" / "beacon"
    if candidate.exists():
        return candidate
    return None


# ---------------------------------------------------------------------------
# Python-native fallbacks (Windows / no-bash environments)
# ---------------------------------------------------------------------------


def _python_dispatch(root: Optional[Path], argv: list[str]) -> int:
    """Minimal Python dispatch for environments without bash.

    Currently handles:
      - `beacon --version` / `beacon -V` / `beacon version`
      - `beacon help` / `beacon --help` / `beacon -h`
      - Any subcommand mapped to a Python entry in `lib/commands.py` via the
        legacy `python3 commands.py <subcmd>` convention.

    Returns the exit code.
    """
    if not argv or argv[0] in ("--version", "-V", "version"):
        print(f"beacon {__version__}")
        return 0

    if argv[0] in ("--help", "-h", "help"):
        _print_help()
        return 0

    if root is None:
        _eprint(
            "Error: beacon installation is incomplete — could not locate the "
            "`lib/` directory. Reinstall with `pipx reinstall beacon` or "
            "ensure the source checkout includes lib/commands.py."
        )
        return 2

    # Native Python dispatch for the subset that commands.py already handles
    # directly. We invoke commands.py the same way bin/beacon does, but with
    # the correct lib/ directory injected into PYTHONPATH so legacy flat
    # imports (`from store import ...`) work in both source and wheel layouts.
    commands_py = _resolve_commands_py(root)
    lib_dir = _resolve_lib_dir(root)
    if commands_py is None or lib_dir is None:
        _eprint(f"Error: cannot locate commands.py under {root}")
        return 2

    cmd = [sys.executable, str(commands_py), *argv]
    env = os.environ.copy()
    env.setdefault("BEACON_PROJECT_FILE", ".beacon/project.json")
    env["BEACON_DIR"] = str(root)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{lib_dir}{os.pathsep}{existing_pp}" if existing_pp else str(lib_dir)
    )
    try:
        return subprocess.call(cmd, env=env)
    except OSError as exc:
        _eprint(f"Error launching python dispatch: {exc}")
        return 2


def _print_help() -> None:
    print(
        f"beacon {__version__} — AI-driven milestone tracker for Claude Code\n"
        "\n"
        "Common commands (cross-platform):\n"
        "  beacon --version              Show version\n"
        "  beacon status                 Show current milestone status\n"
        "  beacon init                   Initialize .beacon/ in current dir\n"
        "  beacon milestone list         List milestones\n"
        "  beacon task add \"desc\"        Add a task\n"
        "\n"
        "Full command list is shown by `beacon help` once the legacy bash\n"
        "dispatcher is available (macOS / Linux only at the moment).\n"
        "Windows full support is tracked under ms-44 task e-695.\n"
    )


# ---------------------------------------------------------------------------
# Bash delegation (macOS / Linux primary path)
# ---------------------------------------------------------------------------


def _bash_dispatch(bash: str, bin_beacon: Path, argv: list[str]) -> int:
    """Delegate to the legacy `bin/beacon` bash script."""
    cmd = [bash, str(bin_beacon), *argv]
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        _eprint(f"Error launching bash dispatch: {exc}")
        return 2


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """Beacon CLI entry-point.

    Resolution:
      1. Find repo root (source / editable / pipx-installed)
      2. If bash available AND bin/beacon present, delegate to it
      3. Otherwise dispatch via Python (handles --version / help / direct
         commands.py invocation)
    """
    if argv is None:
        argv = sys.argv[1:]

    root = _find_repo_root()
    bash = _find_bash()

    # Fast path: --version always works (no repo / bash needed)
    if argv and argv[0] in ("--version", "-V"):
        print(f"beacon {__version__}")
        return 0

    if root is not None and bash is not None:
        bin_beacon = _find_bin_beacon(root)
        if bin_beacon is not None:
            return _bash_dispatch(bash, bin_beacon, argv)

    # Bash unavailable (Windows native) OR bin/beacon missing — fall back
    # to Python dispatch.
    return _python_dispatch(root, argv)


def _eprint(*args, **kwargs) -> None:
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
