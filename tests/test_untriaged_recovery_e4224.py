"""ms-126 / e-4224: untriaged-recovery path + Web API priority contract (e2e).

ms-126 makes priority mandatory: human write-paths reject an empty priority,
machine paths stamp the ``untriaged`` sentinel so the un-judged debt is
*visible* (counted by the ``untriaged-backlog`` trigger) instead of a silent
empty field. The add-path forcing function and the sentinel counting are
already pinned by ``test_untriaged_priority_ms126.py``.

What was NOT contract-tested — and what e-4224 locks in here — is the *other
half* of the loop: once a machine has parked debt as ``untriaged``, a human
must be able to **resolve** it by triaging a real severity, and the debt count
must then drop. Two surfaces carry that recovery:

  A. CLI  ``beacon task/milestone update --priority <sev>`` — the e2e path a
     terminal user takes. Verified through the real ``cmd_*`` env-var handlers
     against a temp project file, then re-counted via
     ``commands._count_untriaged_active`` (the debt source of truth).

  B. Web API — the non-terminal surface (ux-principle-no-terminal). Two
     contracts:
       * create with a *missing* priority → HTTP 400 (human web form must
         supply one of the 5 severities; machine sentinel is not reachable
         from the form).
       * PATCH an untriaged entry/milestone with a real severity → sentinel
         cleared. Before e-4224 the task PATCH model (``EntryUpdate``) had no
         ``priority`` field at all, so a web user could *see* untriaged task
         debt but had no way to clear it without dropping to the terminal —
         a hole exactly where the non-terminal user lives. This test pins the
         now-complete recovery path on both the milestone and the task PATCH.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import commands  # noqa: E402
import core  # noqa: E402


# ===========================================================================
# Part A — CLI recovery e2e (cmd_* env-var handlers against a temp project)
# ===========================================================================


def _write_project(path):
    (path / ".beacon").mkdir(parents=True, exist_ok=True)
    data = {
        "name": "test",
        "milestones": [
            {
                "id": "ms-1",
                "title": "Sample",
                "status": "in_progress",
                # A machine parked this milestone's priority as untriaged debt.
                "priority": core.UNTRIAGED_PRIORITY,
                "progress": 0,
                "target_date": "",
                "entries": [
                    # And an issue-imported task, also untriaged.
                    {
                        "id": "e-1",
                        "type": "task",
                        "description": "machine-imported task",
                        "status": "todo",
                        "meta": {"priority": core.UNTRIAGED_PRIORITY},
                    },
                ],
                "commits": [],
            }
        ],
        "operations": [],
    }
    (path / ".beacon" / "project.json").write_text(json.dumps(data))


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Clean slate: no priority / untriaged / identity env leaking from the dev
    # shell into the handler under test.
    for var in (
        "BEACON_ALLOW_UNTRIAGED",
        "BEACON_SESSION_KIND",
        "BEACON_USER_ID",
        "BEACON_USER_EMAIL",
        "BEACON_DISPLAY_NAME",
        "BEACON_PRIORITY",
        "BEACON_STATUS",
        "BEACON_MS_ID",
        "BEACON_ENTRY_ID",
        "BEACON_DESCRIPTION",
        "BEACON_REASON",
        "BEACON_JSON",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BEACON_HOME", str(tmp_path / ".beacon_home"))
    return tmp_path


def _load(pd):
    return json.loads((pd / ".beacon" / "project.json").read_text())


class TestCliTaskRecovery:
    def test_update_priority_clears_task_sentinel_and_drops_debt(
        self, project_dir, monkeypatch
    ):
        # Baseline: the imported task is counted as untriaged debt.
        before = commands._count_untriaged_active(_load(project_dir))
        assert before == (1, 1)  # (ms untriaged, task untriaged)

        # A human triages the task via `beacon task update e-1 --priority high`.
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
        monkeypatch.setenv("BEACON_PRIORITY", "high")
        commands.cmd_task_update()

        data = _load(project_dir)
        entry = data["milestones"][0]["entries"][0]
        assert entry["meta"]["priority"] == "high"  # sentinel replaced
        # Task debt is gone; only the (still-untriaged) milestone remains.
        assert commands._count_untriaged_active(data) == (1, 0)

    def test_human_cannot_transition_triaged_task_to_untriaged(
        self, project_dir, monkeypatch, capsys
    ):
        # Recovery must move *forward* to a real severity — a human cannot
        # re-park the sentinel. First triage e-1 to "high", then an attempt to
        # transition it back to untriaged is rejected with the precise
        # sentinel-not-a-severity message (AX#3), leaving the value unchanged.
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
        monkeypatch.setenv("BEACON_PRIORITY", "high")
        commands.cmd_task_update()  # triage first
        assert _load(project_dir)["milestones"][0]["entries"][0]["meta"]["priority"] == "high"

        monkeypatch.setenv("BEACON_PRIORITY", core.UNTRIAGED_PRIORITY)
        with pytest.raises(SystemExit) as exc:
            commands.cmd_task_update()
        assert exc.value.code == 1
        assert "sentinel, not a severity" in capsys.readouterr().out
        # Unchanged: still the triaged severity, not re-parked.
        assert (
            _load(project_dir)["milestones"][0]["entries"][0]["meta"]["priority"] == "high"
        )

    def test_omitting_priority_leaves_untriaged_unchanged(
        self, project_dir, monkeypatch
    ):
        # AX round-2: to leave an untriaged task unchanged, you OMIT priority
        # (no --priority) — a status-only edit must not touch the sentinel. This
        # is the correct read-modify-write pattern (omit unchanged fields).
        before = commands._count_untriaged_active(_load(project_dir))
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
        monkeypatch.delenv("BEACON_PRIORITY", raising=False)  # omit priority
        monkeypatch.setenv("BEACON_STATUS", "in_progress")    # the real edit
        commands.cmd_task_update()
        data = _load(project_dir)
        entry = data["milestones"][0]["entries"][0]
        assert entry["status"] == "in_progress"
        assert entry["meta"]["priority"] == core.UNTRIAGED_PRIORITY  # untouched
        assert commands._count_untriaged_active(data) == before

    def test_echoing_untriaged_is_rejected_state_independently(
        self, project_dir, monkeypatch, capsys
    ):
        # AX round-2: explicitly sending "untriaged" is rejected regardless of
        # the current value (state-independent) — a client cannot re-assert the
        # machine sentinel. Even on an already-untriaged task, echoing it is a
        # precise error (not a silent no-op), which is predictable from the
        # surface. The entry is left unchanged.
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
        monkeypatch.setenv("BEACON_PRIORITY", core.UNTRIAGED_PRIORITY)
        with pytest.raises(SystemExit) as exc:
            commands.cmd_task_update()
        assert exc.value.code == 1
        assert "sentinel, not a severity" in capsys.readouterr().out
        assert (
            _load(project_dir)["milestones"][0]["entries"][0]["meta"]["priority"]
            == core.UNTRIAGED_PRIORITY
        )


class TestCliMilestoneRecovery:
    def test_update_priority_clears_ms_sentinel_and_drops_debt(
        self, project_dir, monkeypatch
    ):
        assert commands._count_untriaged_active(_load(project_dir)) == (1, 1)

        # `beacon milestone update ms-1 --priority medium` — a priority-only
        # content edit needs no --reason (not a status/owner decision).
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        monkeypatch.setenv("BEACON_PRIORITY", "medium")
        commands.cmd_milestone_update()

        data = _load(project_dir)
        assert data["milestones"][0]["priority"] == "medium"
        # MS debt cleared; only the (still-untriaged) task remains.
        assert commands._count_untriaged_active(data) == (0, 1)


# ===========================================================================
# Part B — Web API contract (real endpoints, in-memory mock backend)
# ===========================================================================

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_store: dict[str, dict] = {}


def _mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def _mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


@pytest.fixture
def web(monkeypatch):
    """Real create/update endpoints against an in-memory project store.

    Mirrors ``test_ws_broadcast_after_write.py``: patch get/save on both
    firestore_client and store_router (operations.py reads via store_router),
    disable auth, stub the Firestore watcher. Auth is orthogonal to the
    priority contract, so we run it disabled to keep the test focused.
    """
    import store_router

    monkeypatch.setattr(firestore_client, "get_project", _mock_get_project)
    monkeypatch.setattr(firestore_client, "save_project", _mock_save_project)
    monkeypatch.setattr(store_router, "get_project", _mock_get_project)
    monkeypatch.setattr(store_router, "save_project", _mock_save_project)
    monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_auth_enabled", False)
    _store.clear()
    yield TestClient(app_module.app)
    _store.clear()


def _seed(pid: str, milestones: list[dict] | None = None) -> None:
    _store[pid] = {
        "name": "test project",
        "summary": "",
        "owner": "",
        "members": [],
        "milestones": milestones or [],
        "operations": [],
        "schema_version": 1,
    }


def _active_ms(ms_id="ms-1", **extra):
    base = {
        "id": ms_id, "title": "MS", "status": "in_progress",
        "progress": 0, "target_date": "", "entries": [], "commits": [],
    }
    base.update(extra)
    return base


class TestWebCreateRequiresPriority:
    def test_create_task_without_priority_is_400(self, web):
        pid = "p-create-task"
        _seed(pid, milestones=[_active_ms()])
        r = web.post(
            f"/api/projects/{pid}/milestones/ms-1/entries",
            json={"description": "web task, no priority", "type": "task"},
        )
        assert r.status_code == 400, r.text
        assert "Priority is required" in r.text
        # Nothing written.
        assert _store[pid]["milestones"][0]["entries"] == []

    def test_create_milestone_without_priority_is_400(self, web):
        pid = "p-create-ms"
        _seed(pid, milestones=[])
        r = web.post(
            f"/api/projects/{pid}/milestones",
            json={"title": "web MS, no priority"},
        )
        assert r.status_code == 400, r.text
        assert "Priority is required" in r.text
        assert _store[pid]["milestones"] == []

    def test_create_commit_entry_needs_no_priority(self, web):
        # The forcing function is task-only — a commit/note create through the
        # same endpoint must not be dragged into the priority requirement.
        pid = "p-create-commit"
        _seed(pid, milestones=[_active_ms()])
        r = web.post(
            f"/api/projects/{pid}/milestones/ms-1/entries",
            json={"description": "a commit", "type": "commit"},
        )
        assert r.status_code == 200, r.text
        entry = _store[pid]["milestones"][0]["entries"][0]
        assert "priority" not in entry.get("meta", {})


class TestWebRecovery:
    def test_patch_task_priority_clears_untriaged_sentinel(self, web):
        # A machine-imported untriaged task, resolved from the Web UI (the
        # path that did not exist before e-4224 added EntryUpdate.priority).
        pid = "p-recover-task"
        _seed(pid, milestones=[_active_ms(entries=[
            {"id": "e-1", "type": "task", "description": "imported",
             "status": "todo", "meta": {"priority": core.UNTRIAGED_PRIORITY}},
        ])])
        assert commands._count_untriaged_active(_store[pid]) == (0, 1)

        r = web.patch(
            f"/api/projects/{pid}/entries/e-1",
            json={"priority": "high"},
        )
        assert r.status_code == 200, r.text
        entry = _store[pid]["milestones"][0]["entries"][0]
        assert entry["meta"]["priority"] == "high"
        assert commands._count_untriaged_active(_store[pid]) == (0, 0)

    def test_patch_task_transition_to_untriaged_rejected(self, web):
        # The web recovery path, like the CLI, only moves to a real severity;
        # transitioning a triaged task BACK to the sentinel is rejected → 400.
        pid = "p-recover-task-guard"
        _seed(pid, milestones=[_active_ms(entries=[
            {"id": "e-1", "type": "task", "description": "triaged",
             "status": "todo", "meta": {"priority": "low"}},
        ])])
        r = web.patch(
            f"/api/projects/{pid}/entries/e-1",
            json={"priority": core.UNTRIAGED_PRIORITY},
        )
        assert r.status_code == 400, r.text
        assert "sentinel, not a severity" in r.text
        assert _store[pid]["milestones"][0]["entries"][0]["meta"]["priority"] == "low"

    def test_patch_untriaged_task_omit_priority_is_noop(self, web):
        # AX round-2: to leave an untriaged task unchanged, OMIT priority. A
        # status-only PATCH (priority defaults to None = no change) succeeds and
        # preserves the sentinel — this is the correct read-modify-write pattern
        # (send only the fields you change), and None is disclosed in the schema.
        pid = "p-recover-task-omit"
        _seed(pid, milestones=[_active_ms(entries=[
            {"id": "e-1", "type": "task", "description": "imported",
             "status": "todo", "meta": {"priority": core.UNTRIAGED_PRIORITY}},
        ])])
        r = web.patch(
            f"/api/projects/{pid}/entries/e-1",
            json={"status": "in_progress"},  # priority omitted → None → no change
        )
        assert r.status_code == 200, r.text
        entry = _store[pid]["milestones"][0]["entries"][0]
        assert entry["status"] == "in_progress"
        assert entry["meta"]["priority"] == core.UNTRIAGED_PRIORITY
        assert commands._count_untriaged_active(_store[pid]) == (0, 1)

    def test_patch_untriaged_task_explicit_untriaged_is_rejected(self, web):
        # AX round-2: explicitly re-sending "untriaged" is rejected 400 even on an
        # already-untriaged task — state-independent, predictable from the surface
        # (no "sometimes a silent no-op, sometimes a 400" ambiguity). The client
        # is told to omit the field instead.
        pid = "p-recover-task-echo"
        _seed(pid, milestones=[_active_ms(entries=[
            {"id": "e-1", "type": "task", "description": "imported",
             "status": "todo", "meta": {"priority": core.UNTRIAGED_PRIORITY}},
        ])])
        r = web.patch(
            f"/api/projects/{pid}/entries/e-1",
            json={"status": "in_progress", "priority": core.UNTRIAGED_PRIORITY},
        )
        assert r.status_code == 400, r.text
        assert "sentinel, not a severity" in r.text
        # Rejected atomically: neither status nor priority changed.
        entry = _store[pid]["milestones"][0]["entries"][0]
        assert entry["status"] == "todo"
        assert entry["meta"]["priority"] == core.UNTRIAGED_PRIORITY

    def test_patch_task_status_only_leaves_priority_untouched(self, web):
        # Empty priority in the PATCH body = "no change": a status-only edit
        # must not wipe an already-triaged severity.
        pid = "p-recover-task-noop"
        _seed(pid, milestones=[_active_ms(entries=[
            {"id": "e-1", "type": "task", "description": "triaged",
             "status": "todo", "meta": {"priority": "low"}},
        ])])
        r = web.patch(
            f"/api/projects/{pid}/entries/e-1",
            json={"status": "in_progress"},
        )
        assert r.status_code == 200, r.text
        entry = _store[pid]["milestones"][0]["entries"][0]
        assert entry["status"] == "in_progress"
        assert entry["meta"]["priority"] == "low"

    def test_patch_milestone_priority_clears_untriaged_sentinel(self, web):
        pid = "p-recover-ms"
        _seed(pid, milestones=[_active_ms(priority=core.UNTRIAGED_PRIORITY)])
        assert commands._count_untriaged_active(_store[pid]) == (1, 0)

        r = web.patch(
            f"/api/projects/{pid}/milestones/ms-1",
            json={"priority": "medium"},
        )
        assert r.status_code == 200, r.text
        assert _store[pid]["milestones"][0]["priority"] == "medium"
        assert commands._count_untriaged_active(_store[pid]) == (0, 0)
