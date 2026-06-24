"""Tests for ``commands._current_project_id`` cloud-mode fallback (ms-95 / e-2007).

Background
----------
In **cloud mode**, the cached ``.beacon/project.json`` document no longer
embeds the project id (the cloud cut-over moved that field into
``.beacon/cloud.json``). The previous implementation of
``_current_project_id()`` only consulted ``load_project()`` and returned
"" whenever ``id`` / ``project_id`` was missing on the local document.

That silent-empty broke two downstream consumers:

* ``trek show`` cross-project judgement — every scope row got tagged as
  "cross-project" because the current project id never matched.
* The scheduler-tick DM payload — ``target_entries`` came out empty so
  the "do next" DM that wakes the worker AI had no entries to chew on,
  structurally degrading ms-83 (server-side Trek scheduler).

Fix
---
Add a fallback: when the project dict carries neither ``id`` nor
``project_id``, read ``project_id`` from ``.beacon/cloud.json`` (same
helper ``_get_cloud_config_path()`` already used by the cloud commands).

These tests pin the 3 branches of the resolver:

1. Local mode with ``id`` in project.json → returns that id.
2. Cloud mode (no id on project dict, but cloud.json present) →
   returns ``cloud.json``'s ``project_id``.
3. Neither path resolves → returns "" gracefully (i.e. no exception
   bubbles up; callers can keep their empty-string sentinel handling).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


@pytest.fixture
def beacon_dir(monkeypatch):
    """Set up a temp ``.beacon/`` directory the helpers will see.

    ``_get_cloud_config_path()`` walks off ``get_project_file()``'s
    dirname, and ``get_project_file()`` honors the ``BEACON_PROJECT_FILE``
    env var. We point it at our temp project.json so both helpers
    resolve to the same temp ``.beacon/`` dir.
    """
    with tempfile.TemporaryDirectory() as tmp:
        bdir = Path(tmp) / ".beacon"
        bdir.mkdir()
        project_file = bdir / "project.json"
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        # Belt-and-suspenders: the test environment may or may not have
        # BEACON_CLOUD set. We don't want a stray cloud flag to send
        # load_project() over the network in test 1 / test 3.
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.chdir(tmp)
        yield bdir


def _stub_load_project(monkeypatch, data: dict) -> None:
    """Make ``commands.load_project()`` return ``data`` deterministically.

    Bypasses the real ``store.load_project()`` path so the test does not
    spin up a StoreApi (cloud-mode) or hit any file we did not stage.
    """
    monkeypatch.setattr(commands, "load_project", lambda: data)


# ---------------------------------------------------------------------------
# AC 4a — local mode: id in project.json wins
# ---------------------------------------------------------------------------

def test_current_project_id_local_mode_reads_id_from_project_json(
    beacon_dir, monkeypatch
):
    """Local mode: ``id`` on the project dict is returned as-is."""
    _stub_load_project(
        monkeypatch,
        {"name": "test-local", "id": "proj-local-abc", "milestones": []},
    )
    # cloud.json is intentionally absent — we are validating the local
    # branch does NOT depend on the fallback.
    assert not (beacon_dir / "cloud.json").exists()
    assert commands._current_project_id() == "proj-local-abc"


# ---------------------------------------------------------------------------
# AC 4b — cloud mode: cloud.json's project_id is the fallback
# ---------------------------------------------------------------------------

def test_current_project_id_cloud_mode_reads_cloud_json_fallback(
    beacon_dir, monkeypatch
):
    """Cloud mode: project dict has no id → fall back to ``cloud.json``.

    Mirrors the post-cut-over layout where the cached project.json
    document does not embed the id and the project_id lives in
    cloud.json.
    """
    _stub_load_project(
        monkeypatch,
        {"name": "test-cloud", "milestones": []},  # no id / project_id
    )
    (beacon_dir / "cloud.json").write_text(
        json.dumps({
            "project_id": "proj-cloud-xyz",
            "api_url": "https://example.invalid",
        }),
        encoding="utf-8",
    )
    assert commands._current_project_id() == "proj-cloud-xyz"


# ---------------------------------------------------------------------------
# AC 4c — neither source resolves: empty string, no exception
# ---------------------------------------------------------------------------

def test_current_project_id_returns_empty_when_no_source_resolves(
    beacon_dir, monkeypatch
):
    """Defensive path: no id anywhere → "" sentinel, never raises.

    Mirrors a corrupted-bootstrap scenario where neither the project
    dict nor cloud.json carries a project id. Existing callers branch
    on the empty string, so the helper must not raise.
    """
    _stub_load_project(
        monkeypatch,
        {"name": "test-empty", "milestones": []},
    )
    # cloud.json absent on purpose.
    assert not (beacon_dir / "cloud.json").exists()
    assert commands._current_project_id() == ""


# ---------------------------------------------------------------------------
# Bonus: load_project itself raising must not crash the helper.
# Documents the existing ``except Exception`` swallow so a future
# refactor cannot regress it.
# ---------------------------------------------------------------------------

def test_current_project_id_swallows_load_project_exception(
    beacon_dir, monkeypatch
):
    """If ``load_project`` raises, fall through to cloud.json cleanly."""
    def _raise():
        raise RuntimeError("simulated load_project failure")
    monkeypatch.setattr(commands, "load_project", _raise)
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"project_id": "proj-fallback-after-raise"}),
        encoding="utf-8",
    )
    assert commands._current_project_id() == "proj-fallback-after-raise"
