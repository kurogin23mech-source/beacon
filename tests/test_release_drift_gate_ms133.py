"""The release SHIP gate runs the strict drift guards (ms-133).

bash↔Python CLI drift (the Windows `argparse invalid choice` class) is caught on
PRs by lint-docs.yml, but a `push: main` run can't un-push and a PR run only
blocks when it's a required check — so drift can reach main and, from there,
ship in a release. release.yml therefore runs the SAME strict drift guards as an
early gate. These pin that wiring so it can't be silently dropped, and that both
workflows use the ONE shared script (no meta-drift between the two gates).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "ci-strict-drift-guards.sh"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
LINT = ROOT / ".github" / "workflows" / "lint-docs.yml"


def test_shared_guard_script_exists_and_executable():
    assert GUARD.is_file()
    mode = GUARD.stat().st_mode
    assert mode & stat.S_IXUSR, "ci-strict-drift-guards.sh must be executable"


def test_shared_guard_runs_the_cli_drift_check():
    """The script must actually invoke the bash↔Python CLI drift check — that's
    the guard the whole gate exists for."""
    body = GUARD.read_text(encoding="utf-8")
    assert "check-cli-help-drift.py" in body
    assert "--strict" in body


def test_release_workflow_runs_the_guard():
    """release.yml must call the shared guard, or a release could ship drift."""
    assert "ci-strict-drift-guards.sh" in RELEASE.read_text(encoding="utf-8")


def test_lint_docs_workflow_runs_the_same_guard():
    """Both gates use the ONE script, so their blocking-drift list can't diverge."""
    assert "ci-strict-drift-guards.sh" in LINT.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="bash script")
def test_guard_script_passes_on_current_tree():
    """The gate is green on the current tree (a released cut from here is clean)."""
    r = subprocess.run(["bash", str(GUARD)], capture_output=True, text=True,
                       cwd=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr
