"""Tests for bin/beacon-find-root (ms-62 / e-1512).

The walk-up project root finder replaced cwd-only `test -f .beacon/project.json`
checks in the Skill files so that bclaude started inside a worktree (which
shares its parent's .beacon/) still bootstraps correctly.
"""

import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "beacon-find-root"


def _expected_root_from(start: Path) -> Path:
    """Mirror the walk-up logic to compute the expected root from ``start``.

    Tests run from either the main repo (.beacon/project.json exists) or
    from a worktree (no .beacon/, walks up to the main repo). Either way
    the script must agree with this function.
    """
    d = start.resolve()
    while True:
        if (d / ".beacon" / "project.json").is_file():
            return d
        if d.parent == d:
            raise RuntimeError(f"No .beacon/project.json found walking up from {start}")
        d = d.parent


def _run(cwd: Path) -> tuple[int, str]:
    r = subprocess.run(
        [str(SCRIPT)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return r.returncode, r.stdout.strip()


def test_script_is_executable():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_finds_root_from_repo_root(tmp_path):
    # ms-62 e-1512: earlier this test ran against REPO_ROOT directly,
    # which required the developer's local .beacon/project.json to
    # exist. .beacon/ is gitignored so the CI checkout has none — the
    # test then failed with `1 == 0` on every CI run. Build a self-
    # contained fake project tree so the assertion is about the
    # script's walk-up behavior, not about the checkout's dev artifacts.
    project = tmp_path / "myproj"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    code, out = _run(project)
    assert code == 0
    assert Path(out) == project


def test_walks_up_from_subdirectory(tmp_path):
    # Same pattern: build a fake project + sub-dir so the test is
    # independent of the dev checkout's .beacon/ presence.
    project = tmp_path / "myproj"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    sub = project / "lib"
    sub.mkdir()
    code, out = _run(sub)
    assert code == 0
    assert Path(out) == project


def test_walks_up_from_worktree_style_path(tmp_path):
    project = tmp_path / "myproj"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    worktree = project / ".worktrees" / "ms-99-test"
    worktree.mkdir(parents=True)
    code, out = _run(worktree)
    assert code == 0
    assert Path(out) == project


def test_exits_nonzero_outside_beacon(tmp_path):
    code, out = _run(tmp_path)
    assert code == 1
    assert out == ""


def test_handles_path_with_spaces(tmp_path):
    project = tmp_path / "my project with spaces"
    project.mkdir()
    (project / ".beacon").mkdir()
    (project / ".beacon" / "project.json").write_text("{}")
    sub = project / "sub"
    sub.mkdir()
    code, out = _run(sub)
    assert code == 0
    assert Path(out) == project
