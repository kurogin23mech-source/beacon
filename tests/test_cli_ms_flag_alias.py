"""`-m` must be a universal alias for `--ms` across bin/beacon (ms-120 e-3895).

AX 原則 1 (命名の一貫性): the milestone selector split — `--ms` on `milestone`
and status/doc, `-m` on task/log/save — forced AI to remember which command
took which spelling, and passing the wrong one was silently ignored (the L2
memory layer records this failure repeatedly). Both spellings now resolve to the
same milestone everywhere. `--ms` stays the formal name; `-m` is the alias.
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
        json.dumps(
            {
                "name": "t",
                "milestones": [
                    {"id": "ms-1", "title": "alpha", "status": "in_progress",
                     "progress": 10},
                    {"id": "ms-2", "title": "beta", "status": "todo"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _status_ids(proj, selector_flag):
    r = subprocess.run(
        [BASH, str(BIN), "status", selector_flag, "ms-1", "--json"],
        cwd=proj, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return [m["id"] for m in json.loads(r.stdout).get("milestones", [])]


def test_status_dash_m_matches_dash_dash_ms(proj):
    assert _status_ids(proj, "--ms") == ["ms-1"]
    assert _status_ids(proj, "-m") == ["ms-1"]


def test_source_has_no_ms_selector_without_dash_m_alias():
    """Every `--ms)` case arm that selects a milestone must also accept `-m`.

    Guards against re-introducing the split: a bare `--ms)` (not part of a
    `-m|--ms` / `-m|--milestone|--ms` alias group) in a `case "$1" in` arm is a
    regression. (`--ms` appearing inside a usage/help string is ignored — we
    only inspect case-branch labels.)
    """
    offenders = []
    for i, line in enumerate(BIN.read_text(encoding="utf-8").split("\n"), start=1):
        s = line.strip()
        # a case label ends with `)`; the selector arm starts with --ms) or a
        # trailing --ms) in an alias group. We want labels equal to `--ms)`.
        if s.startswith("--ms)"):
            offenders.append((i, s[:60]))
    assert offenders == [], (
        "Bare `--ms)` arms missing the `-m` alias (ms-120 e-3895):\n"
        + "\n".join(f"  L{ln}: {t}" for ln, t in offenders)
    )
