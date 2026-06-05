"""CLI cloud-mode purge tests (ms-14 e-1030).

Local-mode purge (mutates ``.beacon/project.json`` directly) is already
covered by ``test_milestone_purge_cli.py`` and ``test_entry_op_purge.py``.
This file pins the new cloud-mode behavior: when ``_is_cloud_mode()`` is true,
``cmd_*_purge`` must route through the server purge endpoint instead of
writing the local cache + pushing the whole project document.

The tests stub out ``commands._get_api_client`` so the CLI exercises the
full env-var → dispatcher → client call → output path, without any actual
HTTP / Firestore / cloud config required.

Owner-only enforcement itself is tested at the server layer (see
``test_purge_api.py``). Here we just verify the CLI translates a 403 into
a useful message and exit code.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cloud_project_dir(monkeypatch):
    """Temp dir with a .beacon/ folder configured for cloud mode.

    ``BEACON_CLOUD=1`` flips ``_is_cloud_mode()``; ``BEACON_PROJECT_FILE``
    points at a stub project.json so any incidental local-mode probe hits a
    file that actually exists (the cloud branch never reads it for purge).
    """
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "project.json").write_text(
            json.dumps({"name": "stub", "milestones": []}), encoding="utf-8"
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
        monkeypatch.setenv("BEACON_CLOUD", "1")
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        sys.modules.pop("firestore_client", None)
        yield Path(tmp)


def _stub_client(monkeypatch, *, project_data: dict,
                  purge_milestone=None, purge_entry=None, purge_operation=None):
    """Replace ``commands._get_api_client`` with a MagicMock.

    Returns the mock so individual tests can ``assert_called_with`` etc. The
    fake client exposes ``get_project`` (used by the cloud pre-flight) plus
    the three purge methods — each defaults to "happy path" but can be
    overridden per test.
    """
    import commands  # type: ignore

    fake = MagicMock()
    fake.get_project.return_value = project_data
    fake.purge_milestone = purge_milestone or MagicMock(
        return_value={"id": "ms-1", "title": "stub", "purged": True})
    fake.purge_entry = purge_entry or MagicMock(
        return_value={"entry_id": "e-1", "description": "stub", "purged": True})
    fake.purge_operation = purge_operation or MagicMock(
        return_value={"id": "op-1", "title": "stub", "purged": True})

    config = {"project_id": "p-cloud"}

    def _fake_get_api_client():
        return fake, config

    monkeypatch.setattr(commands, "_get_api_client", _fake_get_api_client)
    return fake


# ---------------------------------------------------------------------------
# Milestone purge
# ---------------------------------------------------------------------------

def test_cloud_milestone_purge_calls_api(cloud_project_dir, monkeypatch, capsys):
    """Happy path — single record, no index needed.

    The CLI must call ``client.purge_milestone(project_id, ms_id, reason=...,
    index=None)`` and emit the human-readable purge summary on stdout. No
    local file mutation happens (we'd see SystemExit from load_project_unsafe
    on the stub file if the code accidentally fell through to local mode).
    """
    project_data = {
        "name": "Cloud", "milestones": [
            {"id": "ms-1", "title": "Solo", "status": "in_progress",
             "progress": 0, "target_date": "", "entries": []},
        ],
    }
    fake = _stub_client(monkeypatch, project_data=project_data)

    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_REASON", "test recovery")
    monkeypatch.delenv("BEACON_INDEX", raising=False)

    import commands  # type: ignore
    commands.cmd_milestone_purge()

    fake.purge_milestone.assert_called_once_with(
        "p-cloud", "ms-1", reason="test recovery", index=None,
    )
    out = capsys.readouterr().out
    assert "Purged" in out


def test_cloud_milestone_purge_passes_index_through(cloud_project_dir,
                                                     monkeypatch, capsys):
    """When the project has duplicates and the operator passes --index,
    the CLI must forward that index to the server unchanged."""
    project_data = {
        "name": "Cloud", "milestones": [
            {"id": "ms-99", "title": "first", "status": "todo",
             "progress": 0, "target_date": "", "entries": []},
            {"id": "ms-99", "title": "second", "status": "todo",
             "progress": 0, "target_date": "", "entries": []},
        ],
    }
    fake = _stub_client(monkeypatch, project_data=project_data)

    monkeypatch.setenv("BEACON_MS_ID", "ms-99")
    monkeypatch.setenv("BEACON_REASON", "dup recovery")
    monkeypatch.setenv("BEACON_INDEX", "2")

    import commands  # type: ignore
    commands.cmd_milestone_purge()

    fake.purge_milestone.assert_called_once_with(
        "p-cloud", "ms-99", reason="dup recovery", index=2,
    )


def test_cloud_milestone_purge_duplicate_without_index_exits_with_listing(
        cloud_project_dir, monkeypatch, capsys):
    """Pre-flight UX parity with local mode: when there are 2+ records and
    the operator forgot --index, list them on stderr and exit 1 *without*
    calling the API (no useful server call is possible)."""
    project_data = {
        "name": "Cloud", "milestones": [
            {"id": "ms-99", "title": "First", "status": "todo",
             "progress": 0, "target_date": "", "entries": []},
            {"id": "ms-99", "title": "Second", "status": "todo",
             "progress": 0, "target_date": "", "entries": []},
        ],
    }
    fake = _stub_client(monkeypatch, project_data=project_data)

    monkeypatch.setenv("BEACON_MS_ID", "ms-99")
    monkeypatch.setenv("BEACON_REASON", "ambiguous")
    monkeypatch.delenv("BEACON_INDEX", raising=False)

    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_purge()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "--index 1" in err and "--index 2" in err
    fake.purge_milestone.assert_not_called()


def test_cloud_milestone_purge_translates_403(cloud_project_dir, monkeypatch,
                                                capsys):
    """A 403 from the server should map to a clear "owner only" CLI error
    and exit 1, *not* leak the raw RuntimeError traceback."""
    project_data = {
        "name": "Cloud", "milestones": [
            {"id": "ms-1", "title": "Solo", "status": "in_progress",
             "progress": 0, "target_date": "", "entries": []},
        ],
    }
    fake = _stub_client(
        monkeypatch, project_data=project_data,
        purge_milestone=MagicMock(side_effect=RuntimeError(
            "API error 403: Owner access required")),
    )

    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_REASON", "editor try")

    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_purge()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    # The translated message should mention "owner" without bleeding the
    # generic "API error 403" jargon to the operator.
    assert "owner" in err.lower()
    fake.purge_milestone.assert_called_once()


def test_cloud_milestone_purge_requires_reason(cloud_project_dir, monkeypatch,
                                                capsys):
    """The reason gate is enforced *before* dispatching to cloud — so we don't
    burn a round-trip just to learn the API would have 400'd anyway."""
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_REASON", "")

    # No stub client — the CLI should bail out before calling
    # _get_api_client. If it did try to call, that import would happen
    # naturally and we'd see a different failure mode.
    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_purge()
    assert ei.value.code == 1
    assert "--reason" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Entry purge
# ---------------------------------------------------------------------------

def test_cloud_entry_purge_calls_api(cloud_project_dir, monkeypatch, capsys):
    project_data = {
        "name": "Cloud", "milestones": [
            {"id": "ms-1", "title": "Solo", "status": "in_progress",
             "progress": 0, "target_date": "", "entries": [
                 {"id": "e-1", "type": "task", "description": "x",
                  "status": "todo"},
             ]},
        ],
    }
    fake = _stub_client(monkeypatch, project_data=project_data)

    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "test entry purge")
    monkeypatch.delenv("BEACON_INDEX", raising=False)

    import commands  # type: ignore
    commands.cmd_entry_purge()

    fake.purge_entry.assert_called_once_with(
        "p-cloud", "e-1", reason="test entry purge", index=None,
    )


def test_cloud_entry_purge_translates_403(cloud_project_dir, monkeypatch,
                                            capsys):
    project_data = {
        "name": "Cloud", "milestones": [
            {"id": "ms-1", "title": "Solo", "status": "in_progress",
             "progress": 0, "target_date": "", "entries": [
                 {"id": "e-1", "type": "task", "description": "x",
                  "status": "todo"},
             ]},
        ],
    }
    fake = _stub_client(
        monkeypatch, project_data=project_data,
        purge_entry=MagicMock(side_effect=RuntimeError(
            "API error 403: Owner access required")),
    )

    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "editor try")

    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_entry_purge()
    assert ei.value.code == 1
    assert "owner" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Operation purge
# ---------------------------------------------------------------------------

def test_cloud_operation_purge_calls_api(cloud_project_dir, monkeypatch,
                                          capsys):
    project_data = {
        "name": "Cloud", "milestones": [],
        "operations": [
            {"id": "op-1", "status": "open", "title": "Solo", "entries": []},
        ],
    }
    fake = _stub_client(monkeypatch, project_data=project_data)

    monkeypatch.setenv("BEACON_OP_ID", "op-1")
    monkeypatch.setenv("BEACON_REASON", "test op purge")
    monkeypatch.delenv("BEACON_INDEX", raising=False)

    import commands  # type: ignore
    commands.cmd_operation_purge()

    fake.purge_operation.assert_called_once_with(
        "p-cloud", "op-1", reason="test op purge", index=None,
    )


def test_cloud_operation_purge_translates_403(cloud_project_dir, monkeypatch,
                                                capsys):
    project_data = {
        "name": "Cloud", "milestones": [],
        "operations": [
            {"id": "op-1", "status": "open", "title": "Solo", "entries": []},
        ],
    }
    fake = _stub_client(
        monkeypatch, project_data=project_data,
        purge_operation=MagicMock(side_effect=RuntimeError(
            "API error 403: Owner access required")),
    )

    monkeypatch.setenv("BEACON_OP_ID", "op-1")
    monkeypatch.setenv("BEACON_REASON", "editor try")

    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_operation_purge()
    assert ei.value.code == 1
    assert "owner" in capsys.readouterr().err.lower()
