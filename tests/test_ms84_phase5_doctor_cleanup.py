"""ms-84 Phase 5 tests (e-2039): doctor `project-stale` check retirement.

Pins SPEC AC4: ``beacon doctor`` no longer emits a ``project-stale``
warning. The check function is now a tombstone (= returns []), and the
``warnings.extend(...)`` call in cmd_doctor is commented out.

We verify both surfaces:

1. The helper function ``_doctor_check_project_staleness`` always
   returns an empty list, even on a project structured exactly like the
   pre-Phase-5 trigger case (= local count != cloud count).
2. The cmd_doctor dispatcher does not call the helper anymore — even if
   the local file were stale, the WARN tag would not appear in stdout.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


def test_doctor_check_project_staleness_is_now_noop():
    """SPEC AC4: helper is a tombstone — always returns []."""
    result = commands._doctor_check_project_staleness()
    assert result == []


def test_doctor_check_project_staleness_ignores_drift_condition(monkeypatch, tmp_path):
    """Even when the legacy trigger condition (= local count != cloud count
    + old mtime) is artificially set up, the helper must still return []."""
    bdir = tmp_path / ".beacon"
    bdir.mkdir()
    # Old project.json with 1 milestone + age > 5min via os.utime.
    pf = bdir / "project.json"
    pf.write_text(
        json.dumps({"milestones": [{"id": "ms-1"}], "operations": []}),
        encoding="utf-8",
    )
    # Cloud config + stub API client returning a project with 5 milestones.
    (bdir / "cloud.json").write_text(
        '{"project_id": "p1", "api_url": "https://example"}', encoding="utf-8"
    )
    import time
    old = time.time() - 3600  # 1 h ago — well past 5 min threshold
    os.utime(pf, (old, old))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")

    # Even with a real-looking trigger, the helper must say "all good".
    assert commands._doctor_check_project_staleness() == []


def test_cmd_doctor_does_not_emit_project_stale(monkeypatch, tmp_path):
    """SPEC AC4 end-to-end: full doctor run never includes project-stale."""
    bdir = tmp_path / ".beacon"
    bdir.mkdir()
    pf = bdir / "project.json"
    pf.write_text(
        json.dumps({"name": "phase5", "milestones": [], "operations": []}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    # Avoid any flaky env-dependent checks by forcing skip on optional ones.
    monkeypatch.setenv("BEACON_DOCTOR_SKIP_CLOUD_SYNC", "1")
    monkeypatch.setenv("BEACON_DOCTOR_SKIP_SKILL_DRIFT", "1")

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            commands.cmd_doctor()
        except SystemExit:
            # cmd_doctor exits 0 / 1 — both fine for this assertion.
            pass

    output = buf.getvalue()
    # The tag must not appear. We assert lowercase to be robust to
    # incidental capitalization in error wrappers.
    assert "project-stale" not in output.lower(), (
        "doctor still emits the retired project-stale tag.\n"
        f"output: {output!r}"
    )
