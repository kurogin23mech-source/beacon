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
# stdout/stderr encoding — force UTF-8 (#19)
# ---------------------------------------------------------------------------
#
# Windows PowerShell / cmd default to the system code page (cp932 on a JP
# locale, cp1252 on en-US, …). Printing non-ASCII characters then either
# crashes with `UnicodeEncodeError` (em dash in --help, anything past ASCII
# in Japanese commits) or produces mojibake. Python 3.7+ exposes
# `sys.stdout.reconfigure(encoding=...)` on TextIOWrapper streams, so we
# normalise to UTF-8 once at import time. `errors="replace"` is a safety
# net for the rare terminal that can't render a character: the user sees
# `?` instead of crashing.
#
# This runs at *import* time so every entry-point (the console script,
# `python -m beacon_cli.hooks.post_commit`, test harnesses that import
# main) gets the fix without each call site remembering to repeat it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        # AttributeError: stream wasn't a TextIOWrapper (rare embedding).
        # ValueError/OSError: stream already closed or detached.
        # We silently skip rather than break startup.
        pass


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


def _resolve_scripts_dir(root: Path) -> Optional[Path]:
    """Return the directory holding repo `scripts/*.py` across layouts.

    ms-93 e-3209: the Codex bridge (running from the plugin cache, which does
    NOT ship the daemon scripts) discovers this dir via
    ``beacon __codex-script-root__`` so it can spawn
    ``codex-receive-loop.py`` / ``codex-inbox-hook.py`` from a pipx/brew
    wheel. Source/editable installs expose them at ``<root>/scripts``; the
    wheel remaps them to ``beacon_cli/_bundled_scripts``.
    """
    candidate = root / "scripts"
    if (candidate / "codex-receive-loop.py").exists():
        return candidate
    candidate = root / "_bundled_scripts"
    if (candidate / "codex-receive-loop.py").exists():
        return candidate
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
    """Bash-less dispatcher.

    The Phase-1 (PowerShell Day-1) commands are implemented as a proper
    argparse tree in ``beacon_cli.dispatch`` — that module parses the
    bash-equivalent flags, builds the ``BEACON_*`` env var payload, and
    spawns ``python3 lib/commands.py <subcmd>`` so business logic stays
    in commands.py. We delegate to it here.

    The top-level ``--version`` / ``--help`` fast-path stays in this
    function so they keep working even when ``commands.py`` is unreachable
    (e.g. partial wheel install).
    """
    # Local import: keeps ``main.py`` light when nobody calls into the
    # PowerShell path, and avoids a top-level cycle if dispatch grows.
    from . import dispatch as _dispatch

    if not argv:
        return _dispatch.dispatch(root, argv)
    if argv[0] in ("--version", "-V", "version"):
        print(f"beacon {__version__}")
        return 0
    if argv[0] in ("--help", "-h", "help"):
        # Prefer the Python-native help so users on Windows can read it
        # before any bash dispatch is attempted.
        _print_help()
        return 0
    return _dispatch.dispatch(root, argv)


def _print_help() -> None:
    # Delegate to the dispatcher's help so we have one source of truth for
    # the Phase-1 command list. Keep this function as a thin shim so the
    # existing tests that import `_print_help` keep working.
    from . import dispatch as _dispatch

    _dispatch._print_top_help()


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

    # ms-93 e-3209: hidden helper for the Codex bridge. Print the directory
    # that holds the Codex daemon scripts for THIS install (source `scripts/`
    # or wheel `_bundled_scripts/`). Intercepted before bash delegation so it
    # answers identically for git checkouts and pipx/brew wheels. Prints
    # nothing + exits 1 when the scripts can't be located (partial install).
    if argv and argv[0] == "__codex-script-root__":
        scripts_dir = _resolve_scripts_dir(root) if root is not None else None
        if scripts_dir is None:
            return 1
        print(str(scripts_dir))
        return 0

    # ms-44 e-1311: never delegate to bash on Windows. Git for Windows
    # (...\Git\usr\bin\bash.exe) and WSL (...\System32\bash.exe) put a
    # `bash` on PATH on most Windows dev machines, so the old "bash present
    # => use bin/beacon" heuristic mis-fired: the bash script doesn't force
    # UTF-8 (Japanese output mojibakes) and WSL's bash can't even resolve
    # the Windows-style path to bin/beacon (exit 127). The Python dispatch
    # in dispatch.py *is* the Windows-native path and covers every non-TTY
    # command, so we skip bash entirely here. BEACON_NO_BASH lets power
    # users on any OS force the same Python path for diagnostics.
    use_bash = (
        root is not None
        and bash is not None
        and os.name != "nt"
        and not os.environ.get("BEACON_NO_BASH")
    )
    if use_bash:
        bin_beacon = _find_bin_beacon(root)
        if bin_beacon is not None:
            return _bash_dispatch(bash, bin_beacon, argv)

    # Bash unavailable (Windows native), disabled via BEACON_NO_BASH, or
    # bin/beacon missing — fall back to Python dispatch.
    return _python_dispatch(root, argv)


def _eprint(*args, **kwargs) -> None:
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
