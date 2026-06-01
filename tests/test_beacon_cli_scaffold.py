"""Smoke tests for the beacon_cli Python entry-point scaffold (ms-44 e-695).

These tests verify the Python-level wiring: that `beacon --version` works
without bash, that the repo root resolver finds the source layout, and
that the bundled wheel layout falls back correctly.

They do NOT exercise the full bash dispatch (which is covered by the
existing test suite indirectly via `python3 lib/commands.py ...`).
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make beacon_cli importable from the source tree.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_version_constant_matches_commands_py():
    """The Python entry-point version must match lib/commands.py:__version__."""
    from beacon_cli._version import __version__ as py_version

    # Read commands.py without importing it (commands.py has heavy imports
    # that pull in store, core, etc. — we just want the literal constant).
    commands_py = ROOT / "lib" / "commands.py"
    src = commands_py.read_text(encoding="utf-8")
    import re

    m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    assert m, "lib/commands.py must define __version__"
    bash_version = m.group(1)

    assert py_version == bash_version, (
        f"beacon_cli._version.__version__ ({py_version!r}) and "
        f"lib/commands.py:__version__ ({bash_version!r}) drifted. "
        "Update both when bumping the version."
    )


def test_main_version_flag_prints_version(capsys):
    from beacon_cli.main import main

    exit_code = main(["--version"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "beacon " in out  # "beacon 0.5.0"


def test_main_help_flag_runs():
    """--help should exit 0 regardless of dispatch path (bash or Python).

    Note: we cannot use capsys here because the bash dispatch writes
    directly to fd 1/2 via subprocess.call, bypassing Python's sys.stdout.
    """
    from beacon_cli.main import main

    exit_code = main(["--help"])
    assert exit_code == 0


def test_python_fallback_help_no_bash(capsys, monkeypatch):
    """When bash is unavailable, --help must still work via Python fallback."""
    from beacon_cli import main as main_mod

    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)

    exit_code = main_mod.main(["--help"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "beacon" in out.lower()


def test_find_repo_root_locates_source():
    """In the source checkout, _find_repo_root must return the repo root."""
    from beacon_cli.main import _find_repo_root

    root = _find_repo_root()
    assert root is not None
    assert (root / "lib" / "commands.py").exists() or (
        root / "_bundled_lib" / "commands.py"
    ).exists()


def test_find_bash_returns_path_or_none():
    """_find_bash should return a path on macOS/Linux, None on bash-less envs."""
    from beacon_cli.main import _find_bash

    bash = _find_bash()
    # On the CI runners we use (macOS/ubuntu) bash is always present;
    # on Windows it may not be. Either is correct — just assert the type.
    assert bash is None or isinstance(bash, str)


def test_env_var_override():
    """$BEACON_DIR should win over auto-detection if it points at a valid root."""
    from beacon_cli.main import _find_repo_root

    with mock.patch.dict(os.environ, {"BEACON_DIR": str(ROOT)}):
        root = _find_repo_root()
        assert root == ROOT


def test_env_var_bad_path_falls_back():
    """A bogus $BEACON_DIR should not crash — fall back to auto-detection."""
    from beacon_cli.main import _find_repo_root

    with mock.patch.dict(os.environ, {"BEACON_DIR": "/nonexistent/path/xyz"}):
        root = _find_repo_root()
        # Auto-detection still finds the source root.
        assert root is not None
        assert (root / "lib" / "commands.py").exists() or (
            root / "_bundled_lib" / "commands.py"
        ).exists()
