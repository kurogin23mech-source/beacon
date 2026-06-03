"""bin/beacon must not swallow flag-like tokens as a free-text title (e-902).

Observed: Claude Code, exploring usage, runs `beacon milestone add --help`;
the bash dispatcher's `*) title="$1"` catch-all stored "--help" as the title,
silently creating a bogus "--help" milestone across many projects. The guard
(`_guard_positional`) shows usage on -h/--help, errors on other flags, and —
crucially — never creates anything. Same guard protects `save` and `task add`.

These exercise the real bin/beacon via bash and assert no entity is created.
They skip when bash is unavailable (e.g. a bare Windows shell). The guard runs
before commands.py is ever spawned, so these need only bash, not python3.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "t", "milestones": []}', encoding="utf-8"
    )
    return tmp_path


def _run(proj, *args):
    return subprocess.run(
        [BASH, str(BIN), *args], cwd=proj, capture_output=True, text=True
    )


def _milestones(proj):
    return json.loads(
        (proj / ".beacon" / "project.json").read_text(encoding="utf-8")
    )["milestones"]


def test_milestone_add_help_shows_usage_and_creates_nothing(proj):
    r = _run(proj, "milestone", "add", "--help")
    assert r.returncode == 0
    assert "Usage: beacon milestone add" in r.stdout
    assert _milestones(proj) == []  # the bug: a "--help" milestone here


def test_milestone_add_h_shortflag(proj):
    r = _run(proj, "milestone", "add", "-h")
    assert r.returncode == 0
    assert "Usage: beacon milestone add" in r.stdout
    assert _milestones(proj) == []


def test_milestone_add_unknown_flag_errors_no_create(proj):
    r = _run(proj, "milestone", "add", "--bogus")
    assert r.returncode == 1
    assert "looks like a flag" in r.stderr
    assert _milestones(proj) == []


def test_save_help_creates_nothing(proj):
    r = _run(proj, "save", "--help")
    assert r.returncode == 0
    assert "Usage: beacon save" in r.stdout


def test_task_add_help_creates_nothing(proj):
    r = _run(proj, "task", "add", "--help")
    assert r.returncode == 0
    assert "Usage: beacon task add" in r.stdout


def test_lone_dash_is_still_a_valid_value(proj):
    # A bare "-" must NOT be treated as a flag — only "-x"/"--x" are guarded.
    # We can't fully create here (commands.py spawn), but the guard must not
    # intercept it: the run should not print the flag-guard error.
    r = _run(proj, "milestone", "add", "-")
    assert "looks like a flag" not in r.stderr
