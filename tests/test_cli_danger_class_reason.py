"""Destructive ops require an audit entry; audit-reason flag is unified (e-3906).

AX 原則 6 + audit-trail integrity: destructive / irreversible operations were
inconsistent — milestone/entry purge and member remove required a reason, but
account delete / opportunity delete / meeting cancel / communication cancel made
it optional or absent. They now all require a non-empty --reason OR an explicit
--acknowledge (option B, same gate shape as the state verbs). The three audit
names (--reason / --rationale / --resolution) also accept --reason as the single
canonical spelling.
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
        json.dumps({
            "name": "t",
            "milestones": [],
            "accounts": [{"id": "acc-1", "name": "A", "status": "active"}],
            "opportunities": [{
                "id": "opp-1", "title": "O",
                "meetings": [{"id": "mtg-1", "status": "scheduled", "history": []}],
            }],
        }),
        encoding="utf-8",
    )
    return tmp_path


def _run(proj, *args):
    return subprocess.run([BASH, str(BIN), *args], cwd=proj,
                          capture_output=True, text=True)


@pytest.mark.parametrize("argv", [
    ("account", "delete", "acc-1"),
    ("opportunity", "delete", "opp-1"),
    ("meeting", "cancel", "mtg-1"),
])
def test_destructive_without_reason_is_blocked(proj, argv):
    r = _run(proj, *argv)
    assert r.returncode == 1
    assert "requires an audit entry" in r.stderr


@pytest.mark.parametrize("argv", [
    ("account", "delete", "acc-1"),
    ("opportunity", "delete", "opp-1"),
    ("meeting", "cancel", "mtg-1"),
])
def test_destructive_with_acknowledge_proceeds(proj, argv):
    r = _run(proj, *argv, "--acknowledge")
    assert r.returncode == 0, r.stderr


def test_meeting_cancel_records_the_reason(proj):
    r = _run(proj, "meeting", "cancel", "mtg-1", "--reason", "client rescheduled")
    assert r.returncode == 0, r.stderr
    data = json.loads((proj / ".beacon" / "project.json").read_text("utf-8"))
    mtg = data["opportunities"][0]["meetings"][0]
    assert mtg["status"] != "scheduled"
    assert mtg.get("cancel_reason") == "client rescheduled"


def test_reason_is_a_universal_alias_in_source():
    """--reason must be accepted everywhere --rationale / --resolution are, so an
    AI never has to remember three names for the same audit field (原則 1)."""
    src = BIN.read_text(encoding="utf-8")
    import re
    # every `--rationale)` / `--resolution)` case label must include `--reason`
    offenders = []
    for m in re.finditer(r'^\s*(--(?:rationale|resolution)[^)]*)\)', src, re.M):
        label = m.group(1)
        if "--reason" not in label:
            offenders.append(label)
    assert offenders == [], (
        "these audit-reason case labels lack the --reason alias (e-3906): "
        + ", ".join(offenders)
    )
