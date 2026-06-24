"""Tests for ms-95 / e-2339 — `beacon cloud migrate-from-local`.

Validates two layers:

  1. ``_local_vs_cloud_pre_flight`` — pure structural comparison between
     a local project.json snapshot and the cloud-side project, surfacing
     anything local-only that would be lost on retire.
  2. ``cmd_cloud_migrate_from_local`` — the CLI flow that wires pre-flight
     into the existing ``_rename_local_project_json_for_cloud_cutover``
     helper, gated by a two-factor confirm (= `--confirm <project_id>`).

The cloud round-trip (api_client.get_project) is mocked at the api_client
module boundary so the unit tests stay network-free.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# _local_vs_cloud_pre_flight — pure structural comparison
# ---------------------------------------------------------------------------

def test_pre_flight_pass_when_local_is_subset_of_cloud():
    """The expected happy path: cloud has at least every milestone, task,
    and operation that local has. Retire is safe."""
    local = {
        "name": "p",
        "milestones": [
            {"id": "ms-1", "entries": [{"id": "e-1"}, {"id": "e-2"}]},
        ],
        "operations": [
            {"id": "op-1", "entries": [{"id": "e-3"}]},
        ],
    }
    cloud = {
        "name": "p",
        "milestones": [
            {"id": "ms-1", "entries": [{"id": "e-1"}, {"id": "e-2"},
                                       {"id": "e-99"}]},  # cloud has extra, ok
            {"id": "ms-2", "entries": []},  # cloud has whole extra ms, ok
        ],
        "operations": [
            {"id": "op-1", "entries": [{"id": "e-3"}, {"id": "e-100"}]},
        ],
    }
    issues = commands._local_vs_cloud_pre_flight(local, cloud)
    assert issues == []


def test_pre_flight_catches_milestone_missing_from_cloud():
    """A milestone that exists only locally would be lost on retire."""
    local = {
        "milestones": [{"id": "ms-1", "entries": []},
                       {"id": "ms-2", "entries": []}],
    }
    cloud = {"milestones": [{"id": "ms-1", "entries": []}]}
    issues = commands._local_vs_cloud_pre_flight(local, cloud)
    assert len(issues) == 1
    assert "ms-2" in issues[0]
    assert "missing in cloud" in issues[0]


def test_pre_flight_catches_entry_missing_from_shared_milestone():
    """Entry-level mismatch inside a shared milestone is the most common
    failure mode (= local had an extra commit that never synced)."""
    local = {
        "milestones": [
            {"id": "ms-1", "entries": [{"id": "e-1"}, {"id": "e-2"}]},
        ],
    }
    cloud = {
        "milestones": [
            {"id": "ms-1", "entries": [{"id": "e-1"}]},
        ],
    }
    issues = commands._local_vs_cloud_pre_flight(local, cloud)
    assert any("ms-1" in i and "e-2" in i for i in issues)


def test_pre_flight_catches_operation_mismatch():
    """Operations get the same treatment as milestones."""
    local = {"operations": [{"id": "op-x", "entries": []}]}
    cloud = {"operations": []}
    issues = commands._local_vs_cloud_pre_flight(local, cloud)
    assert any("op-x" in i for i in issues)


def test_pre_flight_catches_name_mismatch():
    """A name mismatch usually means the local file belongs to a
    different project — a strong signal to refuse retire."""
    local = {"name": "alpha", "milestones": []}
    cloud = {"name": "beta", "milestones": []}
    issues = commands._local_vs_cloud_pre_flight(local, cloud)
    assert any("project name" in i for i in issues)


def test_pre_flight_tolerates_missing_fields():
    """Empty or absent collections must not crash the comparison."""
    assert commands._local_vs_cloud_pre_flight({}, {}) == []
    assert commands._local_vs_cloud_pre_flight(
        {"milestones": None}, {"milestones": None}
    ) == []


# ---------------------------------------------------------------------------
# cmd_cloud_migrate_from_local — end-to-end CLI flow
# ---------------------------------------------------------------------------

@pytest.fixture
def cloud_mode_project(tmp_path, monkeypatch):
    """A project that is in cloud mode AND has an orphan local project.json.

    This is the exact state e-2339 targets: cloud.json exists (= cut-over
    already happened) but project.json was never renamed because the
    historical cut-over predates the rename helper.
    """
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    project = {
        "name": "test-project",
        "milestones": [{"id": "ms-1", "entries": [{"id": "e-1"}]}],
        "operations": [],
    }
    (beacon_dir / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2))
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"project_id": "test-pid-123"}))
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_migrate_refuses_without_confirm(cloud_mode_project, monkeypatch,
                                          capsys):
    """No --confirm → bail out before touching anything (e-1776 silent-
    flip incident protection mirrored from `cloud off`)."""
    monkeypatch.delenv("BEACON_CONFIRM", raising=False)
    monkeypatch.delenv("BEACON_FORCE", raising=False)
    with pytest.raises(SystemExit):
        commands.cmd_cloud_migrate_from_local()
    out = capsys.readouterr().out
    assert "two-factor confirmation" in out
    # Local file untouched
    assert (cloud_mode_project / ".beacon" / "project.json").exists()


def test_migrate_refuses_when_confirm_does_not_match(cloud_mode_project,
                                                     monkeypatch, capsys):
    """Wrong project_id in --confirm → bail. Same protection."""
    monkeypatch.setenv("BEACON_CONFIRM", "wrong-pid")
    with pytest.raises(SystemExit):
        commands.cmd_cloud_migrate_from_local()
    out = capsys.readouterr().out
    assert "two-factor confirmation" in out
    assert (cloud_mode_project / ".beacon" / "project.json").exists()


def test_migrate_refuses_when_not_in_cloud_mode(tmp_path, monkeypatch,
                                                capsys):
    """No cloud.json → nothing to migrate, error out clearly."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text("{}")
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_CONFIRM", "anything")
    with pytest.raises(SystemExit):
        commands.cmd_cloud_migrate_from_local()
    out = capsys.readouterr().out
    assert "Not in cloud mode" in out


def test_migrate_noop_when_local_already_retired(tmp_path, monkeypatch,
                                                 capsys):
    """No local project.json → nothing to do, success exit."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"project_id": "p"}))
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_CONFIRM", "p")
    # Should NOT raise SystemExit
    commands.cmd_cloud_migrate_from_local()
    out = capsys.readouterr().out
    assert "nothing to migrate" in out


def test_migrate_happy_path_renames_local_to_dated_suffix(cloud_mode_project,
                                                          monkeypatch,
                                                          capsys):
    """Confirm matches + pre-flight passes → local renamed to .before-cloud-
    YYYYMMDD via the existing helper."""
    monkeypatch.setenv("BEACON_CONFIRM", "test-pid-123")
    monkeypatch.delenv("BEACON_FORCE", raising=False)

    _install_cloud_stubs(monkeypatch,
                         remote={"name": "test-project",
                                 "milestones": [
                                     {"id": "ms-1",
                                      "entries": [{"id": "e-1"}]},
                                 ],
                                 "operations": []})

    commands.cmd_cloud_migrate_from_local()
    out = capsys.readouterr().out
    assert "Retired:" in out
    # Original gone, dated rename present
    orig = cloud_mode_project / ".beacon" / "project.json"
    assert not orig.exists()
    renamed = list((cloud_mode_project / ".beacon").glob(
        "project.json.before-cloud-*"))
    assert len(renamed) == 1


def test_migrate_aborts_when_pre_flight_finds_local_only_entries(
        cloud_mode_project, monkeypatch, capsys):
    """Pre-flight catches a local entry that cloud is missing → refuse
    retire so the user can recover the value first."""
    monkeypatch.setenv("BEACON_CONFIRM", "test-pid-123")
    monkeypatch.delenv("BEACON_FORCE", raising=False)

    _install_cloud_stubs(monkeypatch,
                         remote={"name": "test-project",
                                 "milestones": [],  # cloud knows nothing
                                 "operations": []})

    with pytest.raises(SystemExit):
        commands.cmd_cloud_migrate_from_local()
    out = capsys.readouterr().out
    assert "Pre-flight failed" in out
    assert "ms-1" in out
    # Local file untouched
    assert (cloud_mode_project / ".beacon" / "project.json").exists()


def test_migrate_force_after_review_overrides_pre_flight(cloud_mode_project,
                                                         monkeypatch,
                                                         capsys):
    """When the user has audited the local-only entries and accepts that
    they are dead residue, --force-after-review allows the retire to
    proceed (the rename keeps the dated backup so recovery is still
    possible)."""
    monkeypatch.setenv("BEACON_CONFIRM", "test-pid-123")
    monkeypatch.setenv("BEACON_FORCE", "1")

    _install_cloud_stubs(monkeypatch,
                         remote={"name": "test-project",
                                 "milestones": [],
                                 "operations": []})

    commands.cmd_cloud_migrate_from_local()
    out = capsys.readouterr().out
    assert "Override accepted" in out
    assert "Retired:" in out
    renamed = list((cloud_mode_project / ".beacon").glob(
        "project.json.before-cloud-*"))
    assert len(renamed) == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _install_cloud_stubs(monkeypatch, *, remote):
    """Install a fake api_client.ApiClient that returns ``remote`` from
    get_project, plus a benign auth + url stub."""

    class _StubClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_project(self, project_id):
            return dict(remote)

    fake_api = type(sys)("api_client")
    fake_api.ApiClient = _StubClient
    monkeypatch.setitem(sys.modules, "api_client", fake_api)

    class _FakeAuth:
        def load_credentials(self):
            return {"token": "stub"}

    monkeypatch.setitem(sys.modules, "auth", _FakeAuth())
    monkeypatch.setattr(commands, "_resolve_active_api_url",
                        lambda: "https://test", raising=False)
    monkeypatch.setattr(commands, "_extract_token", lambda _c: "stub-token",
                        raising=False)
