"""CLI-level binding of `beacon project cleanup --confirm` to the reviewed set
(ms-125 e-4095).

The pure helper ``bind_confirmed_plan`` is unit-tested in
``test_project_cleanup.py``; this file drives ``commands.cmd_project_cleanup``
end-to-end (with the cloud client stubbed) to pin the CLI contract the human
actually hits:

  * ``--confirm`` without ``--ids`` fails closed (never archives a recomputed
    set).
  * ``--confirm --ids <a,b>`` archives EXACTLY those ids, and a project that
    became a candidate after the dry-run (not in --ids) is refused, not swept
    up.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


class _StubClient:
    """Stub cloud client: canned project list, records archive calls."""

    def __init__(self, projects):
        self._projects = projects
        self.archived: list[str] = []

    def list_projects(self):
        return self._projects

    def issue_bus_envelope(self, pid, *, tier, actions_authorized=None):
        return {"tier": tier, "signature": "stub-sig",
                "actions_authorized": list(actions_authorized or [])}

    def archive_project(self, pid, env):
        self.archived.append(pid)
        return {"ok": True}


def _wire(monkeypatch, projects):
    """Route cmd_project_cleanup's cloud plumbing to a stub client."""
    stub = _StubClient(projects)
    import auth
    import api_client
    monkeypatch.setattr(auth, "load_credentials", lambda: {"token": "t"})
    monkeypatch.setattr(commands, "_resolve_active_api_url", lambda: "http://x")
    monkeypatch.setattr(commands, "_extract_token", lambda creds: "t")
    monkeypatch.setattr(api_client, "ApiClient", lambda url, tok: stub)
    monkeypatch.setattr(
        commands, "_resolve_current_project_id_from_cloud_json", lambda: "")
    return stub


def _reset_env(monkeypatch):
    for k in ("BEACON_CLEANUP_CONFIRM", "BEACON_CLEANUP_CONFIRM_IDS",
              "BEACON_CLEANUP_LIMIT", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


def test_confirm_without_ids_fails_closed(monkeypatch, capsys):
    projects = [{"project_id": "phase4-test-a", "name": "phase4-test",
                 "owner": "u", "archived": False}]
    stub = _wire(monkeypatch, projects)
    _reset_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM", "1")  # no --ids
    with pytest.raises(SystemExit) as ei:
        commands.cmd_project_cleanup()
    assert ei.value.code == 1
    assert stub.archived == []  # nothing archived
    assert "--ids" in capsys.readouterr().err


def test_confirm_binds_to_ids_and_refuses_new(monkeypatch, capsys):
    # The human reviewed a + b in the dry-run; 'z' became a candidate since.
    projects = [
        {"project_id": "phase4-test-a", "name": "phase4-test",
         "owner": "u", "archived": False},
        {"project_id": "phase4-test-b", "name": "phase4-test",
         "owner": "u", "archived": False},
        {"project_id": "phase4-test-z", "name": "phase4-test",
         "owner": "u", "archived": False},
    ]
    stub = _wire(monkeypatch, projects)
    _reset_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM", "1")
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM_IDS", "phase4-test-a,phase4-test-b")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_project_cleanup()
    out = json.loads(capsys.readouterr().out)
    # Only the reviewed ids were archived; 'z' refused (never shown to human).
    assert set(stub.archived) == {"phase4-test-a", "phase4-test-b"}
    assert out["refused_unreviewed"] == ["phase4-test-z"]
    assert "phase4-test-z" not in stub.archived


def test_confirmed_id_no_longer_candidate_is_skipped(monkeypatch, capsys):
    # Human confirmed 'x' but it's gone (already archived server-side).
    projects = [{"project_id": "phase4-test-a", "name": "phase4-test",
                 "owner": "u", "archived": False}]
    stub = _wire(monkeypatch, projects)
    _reset_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM", "1")
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM_IDS", "phase4-test-a,phase4-test-x")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_project_cleanup()
    out = json.loads(capsys.readouterr().out)
    assert stub.archived == ["phase4-test-a"]
    assert out["skipped_missing"] == ["phase4-test-x"]


def test_dry_run_emits_paste_ready_ids(monkeypatch, capsys):
    projects = [{"project_id": "phase4-test-a", "name": "phase4-test",
                 "owner": "u", "archived": False}]
    _wire(monkeypatch, projects)
    _reset_env(monkeypatch)  # no confirm → dry-run
    commands.cmd_project_cleanup()
    out = capsys.readouterr().out
    assert "--confirm --ids phase4-test-a" in out
    assert "DRY-RUN" in out


def test_ids_without_confirm_fails_closed(monkeypatch, capsys):
    # ms-125 review (AX high): --ids on a dry-run is not a silent no-op.
    projects = [{"project_id": "phase4-test-a", "name": "phase4-test",
                 "owner": "u", "archived": False}]
    stub = _wire(monkeypatch, projects)
    _reset_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM_IDS", "phase4-test-a")  # no --confirm
    with pytest.raises(SystemExit) as ei:
        commands.cmd_project_cleanup()
    assert ei.value.code == 1
    assert stub.archived == []
    assert "ids" in capsys.readouterr().err.lower()


def test_confirm_limit_reports_dropped_ids(monkeypatch, capsys):
    # ms-125 review (AX): ids cut by --limit are reported, not silently skipped.
    projects = [
        {"project_id": f"phase4-test-{i}", "name": "phase4-test",
         "owner": "u", "archived": False} for i in ("a", "b", "c")
    ]
    stub = _wire(monkeypatch, projects)
    _reset_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM", "1")
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM_IDS",
                       "phase4-test-a,phase4-test-b,phase4-test-c")
    monkeypatch.setenv("BEACON_CLEANUP_LIMIT", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_project_cleanup()
    out = json.loads(capsys.readouterr().out)
    assert stub.archived == ["phase4-test-a"]
    assert set(out["dropped_by_limit"]) == {"phase4-test-b", "phase4-test-c"}


def test_ids_required_json_error_carries_hint(monkeypatch, capsys):
    # ms-125 review (AX low): the JSON error tells an automated caller HOW to
    # recover, not just that ids are missing.
    projects = [{"project_id": "phase4-test-a", "name": "phase4-test",
                 "owner": "u", "archived": False}]
    _wire(monkeypatch, projects)
    _reset_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLEANUP_CONFIRM", "1")  # confirm but no ids
    monkeypatch.setenv("BEACON_JSON", "1")
    with pytest.raises(SystemExit):
        commands.cmd_project_cleanup()
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "ids_required"
    assert "hint" in out and out["hint"]
