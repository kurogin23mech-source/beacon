"""Endpoint integration for the phantom-done warning gate (ms-95 / e-2726).

Verifies that ``POST /api/projects/{p}/entries/{e}/done`` returns the
expected ``phantom_done_warning`` payload when the just-done task has no
matching commit in the recent window, and omits it when commit evidence
exists. Also reproduces the two real phantom-done cases observed in the
2026-06-28 dogfood (= e-710-style PE residue + e-2567-style AC drift).

Note on storage backend: this file mirrors the firestore_client mocking
pattern from ``tests/test_api.py`` (= ``BEACON_OPERATIONS_BACKEND=mock``
+ in-memory ``_store``). ``operations.load_project_consistent`` reads
through ``store_router`` which is also mocked, so the endpoint sees the
same data that was just written by ``apply_operation``.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Must be set BEFORE importing operations / app.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # type: ignore  # noqa: E402

_store: dict[str, dict] = {}


def _mock_get_project(project_id: str):
    return copy.deepcopy(_store.get(project_id)) if project_id in _store else None


def _mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def _mock_list_projects():
    return [
        {"project_id": pid, "name": data.get("name", "")}
        for pid, data in _store.items()
    ]


def _mock_list_documents(project_id: str):
    return []


firestore_client.get_project = _mock_get_project
firestore_client.save_project = _mock_save_project
firestore_client.list_projects = _mock_list_projects
firestore_client.list_documents = _mock_list_documents

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

# Mirror onto store_router (= db inside app.py) for the same reason
# tests/test_api.py does — load_project_consistent reads via store_router.
_store_router = app_module.db
_store_router.get_project = _mock_get_project
_store_router.save_project = _mock_save_project
_store_router.list_projects = _mock_list_projects
_store_router.list_documents = _mock_list_documents

app_module._auth_enabled = False
client = TestClient(app_module.app)

PROJECT_ID = "phantom-test"


def _seed(milestones):
    _store.clear()
    _store[PROJECT_ID] = {"name": "Phantom Test", "milestones": milestones}


def _task(eid: str, *, description: str = "do the work",
          acceptance_criteria: str = "", status: str = "todo"):
    e = {
        "id": eid,
        "type": "task",
        "description": description,
        "status": status,
        "date": "2026-07-01",
        "created_at": "2026-07-01",
        "done_at": None,
        "meta": {},
    }
    if acceptance_criteria:
        e["acceptance_criteria"] = acceptance_criteria
    return e


def _commit(eid: str, message: str, *, created_at: str = "2026-07-01T12:00:00Z",
            commit_hash: str = "abc1234"):
    return {
        "id": eid,
        "type": "commit",
        "description": message,
        "status": "done",
        "date": created_at,
        "created_at": created_at,
        "done_at": created_at,
        "meta": {"hash": commit_hash, "message": message},
    }


@pytest.fixture(autouse=True)
def _reset():
    # Re-apply mocks each test in case sibling tests leaked.
    firestore_client.get_project = _mock_get_project
    firestore_client.save_project = _mock_save_project
    firestore_client.list_projects = _mock_list_projects
    firestore_client.list_documents = _mock_list_documents
    _store_router.get_project = _mock_get_project
    _store_router.save_project = _mock_save_project
    _store_router.list_projects = _mock_list_projects
    _store_router.list_documents = _mock_list_documents
    yield
    _store.clear()


# ---------------------------------------------------------------------------
# Phantom done warning emission
# ---------------------------------------------------------------------------


def test_phantom_done_no_commits_emits_warning():
    """Task done in a project with zero commit history flags as phantom."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task("e-100", description="phantom evidence gate keyword"),
            ],
        },
    ])
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-100/done")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert "phantom_done_warning" in body, body
    w = body["phantom_done_warning"]
    assert w["task_id"] == "e-100"
    assert w["match_type"] == "none"


def test_e710_style_phantom_done_flagged():
    """e-710 reproduction — task done with completely unrelated commits."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task(
                    "e-710",
                    description="LPS dev impl complete",
                    acceptance_criteria="implementation finished",
                ),
                _commit("e-901", "chore: bump version to 0.51.4"),
                _commit("e-902", "docs: clarify README"),
            ],
        },
    ])
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-710/done")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert "phantom_done_warning" in body
    assert body["phantom_done_warning"]["match_type"] == "none"


def test_e2567_style_drift_flagged():
    """e-2567 reproduction — AC mentions broad sweep, no matching commit."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task(
                    "e-2567",
                    description="iterate every scope entry, broad sweep",
                    acceptance_criteria="loops across all entries",
                ),
                _commit("e-901", "fix typo in README"),
            ],
        },
    ])
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-2567/done")
    assert r.status_code == 200
    assert "phantom_done_warning" in r.json()


def test_id_referenced_commit_suppresses_warning():
    """Commit message that names the task id → no warning."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task("e-200", description="wire phantom gate"),
                _commit(
                    "e-903",
                    "feat(ms-95): e-200 — wire phantom-done evidence gate",
                ),
            ],
        },
    ])
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-200/done")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert "phantom_done_warning" not in body, body


def test_keyword_overlap_commit_suppresses_warning():
    """Commit message with high keyword overlap → no warning, even without id ref."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task(
                    "e-300",
                    description="phantom evidence cloudlogging gate keyword",
                ),
                _commit(
                    "e-904",
                    "wire phantom evidence cloudlogging gate; new keyword flow",
                ),
            ],
        },
    ])
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-300/done")
    assert r.status_code == 200
    assert "phantom_done_warning" not in r.json()


def test_empty_description_does_not_warn():
    """A task with no description / AC is unjudgeable — no false warning."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task("e-400", description=""),
            ],
        },
    ])
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-400/done")
    assert r.status_code == 200
    assert "phantom_done_warning" not in r.json()


def test_warning_emitted_to_audit_logger():
    """The phantom warning lands on the beacon.audit logger at WARNING level.

    Note: ``beacon.audit`` has ``propagate=False`` so pytest's caplog
    fixture cannot see it via the root handler. Attach a list-collecting
    handler directly on the named logger.
    """
    import logging

    captured: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record):  # noqa: D401 — log handler protocol
            try:
                captured.append(self.format(record))
            except Exception:
                pass

    audit_logger = logging.getLogger("beacon.audit")
    handler = _Collector(level=logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(handler)

    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task(
                    "e-500",
                    description="unique-token phantom marker for caplog test",
                ),
            ],
        },
    ])
    try:
        r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-500/done")
    finally:
        audit_logger.removeHandler(handler)

    assert r.status_code == 200
    payloads = [m for m in captured if "phantom_done_warning" in m]
    assert payloads, captured
    assert "task.done.phantom_done_warning" in payloads[0]
    assert '"task_id": "e-500"' in payloads[0]


def test_gate_env_disable():
    """``BEACON_PHANTOM_DONE_GATE=0`` removes the warning emission entirely."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task("e-600", description="unrelated work"),
            ],
        },
    ])
    old = os.environ.get("BEACON_PHANTOM_DONE_GATE")
    os.environ["BEACON_PHANTOM_DONE_GATE"] = "0"
    try:
        r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-600/done")
    finally:
        if old is None:
            os.environ.pop("BEACON_PHANTOM_DONE_GATE", None)
        else:
            os.environ["BEACON_PHANTOM_DONE_GATE"] = old
    assert r.status_code == 200
    assert "phantom_done_warning" not in r.json()


def test_done_still_succeeds_even_when_flagged():
    """Flag, not filter — done succeeds and task status flips to 'done'."""
    _seed([
        {
            "id": "ms-1",
            "title": "MS",
            "status": "in_progress",
            "target_date": "",
            "commits": [],
            "entries": [
                _task("e-700", description="orphan task"),
            ],
        },
    ])
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-700/done")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    # warning is present...
    assert "phantom_done_warning" in body
    # ...but the underlying state was actually mutated.
    stored = _store[PROJECT_ID]
    task = stored["milestones"][0]["entries"][0]
    assert task["status"] == "done"
    assert task.get("done_at")
