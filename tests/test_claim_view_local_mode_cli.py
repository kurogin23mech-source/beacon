"""ms-125 e-4092 (ms-112 目的達成レビュー指摘): `beacon claim view` must degrade
gracefully in local mode (no cloud.json), not exit 1.

Background: `_resolve_healthy_session_ids` intends to collapse any liveness-check
failure to None ("assume occupation claims are live"), but it caught only
`Exception`. `_get_api_client()` aborts with `sys.exit(1)` (= SystemExit, a
BaseException) when cloud.json is absent, so the whole command died in local
mode. The hermetic integration test stubbed `_resolve_healthy_session_ids` and
so never exercised the real degraded path. This runs the REAL CLI in a
local-mode project to guard the bash↔python↔auth seam.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

BEACON = Path(__file__).resolve().parent.parent / "bin" / "beacon"


@pytest.fixture
def local_project(tmp_path):
    """Local-mode project: project.json only, NO cloud.json — liveness cannot be
    verified, so the claim view must degrade rather than crash."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "ClaimViewLocalMode",
        "summary": "",
        "milestones": [{
            "id": "ms-1", "title": "p", "status": "in_progress",
            "target_date": "", "commits": [], "entries": [],
            "assignee": "alice",
        }],
    }))
    return tmp_path


def _run(cwd, *args):
    env = dict(os.environ)
    # Ensure we exercise the real degraded path, not a stray cloud config.
    env.pop("BEACON_API_URL", None)
    return subprocess.run(
        ["bash", str(BEACON), *args],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=30,
    )


def test_claim_view_all_targets_degrades_in_local_mode(local_project):
    r = _run(local_project, "claim", "view", "--json")
    combined = r.stdout + r.stderr
    assert r.returncode == 0, combined
    assert "No cloud.json" not in r.stdout, combined  # no crash message on stdout path
    # Output is valid JSON (the view built off occupation claims / assignee).
    data = json.loads(r.stdout)
    assert isinstance(data, (dict, list)), combined


def test_claim_view_single_target_degrades_in_local_mode(local_project):
    r = _run(local_project, "claim", "view", "--target", "ms:ms-1", "--json")
    combined = r.stdout + r.stderr
    assert r.returncode == 0, combined
    data = json.loads(r.stdout)
    # assignee layer still surfaces even though liveness is unverifiable.
    assert "alice" in json.dumps(data, ensure_ascii=False), combined
