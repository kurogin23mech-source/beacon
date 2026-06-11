"""Tests for the Python entry-point of beacon-find-root (ms-62 / e-1512).

The Python entry-point is what ``pip install beacon`` exposes on PATH as
``beacon-find-root``. The Skills (= skills/*.md) call it as their bootstrap
prerequisite check, so it must agree with the shell version
(``bin/beacon-find-root``).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beacon_cli.find_root import find_beacon_root, main


def test_find_beacon_root_walks_up_from_subdir(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    sub = project / "sub" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert find_beacon_root() == project


def test_find_beacon_root_handles_worktree_layout(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    worktree = project / ".worktrees" / "ms-99"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)
    assert find_beacon_root() == project


def test_find_beacon_root_returns_none_outside_beacon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert find_beacon_root() is None


def test_main_exit_zero_on_hit(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    monkeypatch.chdir(project)
    rc = main()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == str(project)


def test_main_exit_one_on_miss(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main()
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""


def test_shell_and_python_versions_agree(tmp_path, monkeypatch):
    """Run both implementations against the same cwd, assert the same result."""
    import subprocess
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    sub = project / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    # Python version
    py_root = find_beacon_root()
    # Shell version
    repo_root = Path(__file__).resolve().parent.parent
    shell_script = repo_root / "bin" / "beacon-find-root"
    if not shell_script.is_file() or not os.access(shell_script, os.X_OK):
        pytest.skip("shell helper not present / not executable")
    proc = subprocess.run(
        [str(shell_script)],
        cwd=str(sub),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.fail(f"shell helper exited {proc.returncode}")
    shell_root = Path(proc.stdout.strip())
    assert py_root == shell_root
