"""WebSocket broadcast hydrates milestones from v2 subcollection (ms-43 e-1473).

Locks in the contract that ``_on_snapshot`` re-hydrates milestones from the
``projects/{id}/milestones`` subcollection before broadcasting, so a meta-only
update on the parent doc (summary / members / heartbeat ping) does not push a
``{milestones: []}`` frame and clear the WebUI's milestone list until reload.

Bug pre-fix: ``_on_snapshot`` passed ``doc.to_dict()`` straight to
``_broadcast``. v2 parent docs no longer carry ``milestones[]``, so any
non-milestone write triggered an empty-list broadcast — observed as
"milestones disappear, refresh brings them back" in the WebUI.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import app as app_module  # noqa: E402


class _StubSnap:
    def __init__(self, payload: dict):
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


class _StubCollection:
    def __init__(self, snaps: list[_StubSnap]):
        self._snaps = snaps

    def stream(self):
        return iter(self._snaps)


class _StubDoc:
    def __init__(self, milestones_snaps: list[_StubSnap]):
        self._ms = _StubCollection(milestones_snaps)

    def collection(self, name: str):
        assert name == "milestones", f"unexpected subcollection: {name}"
        return self._ms


class _StubProjects:
    def __init__(self, doc_stub: _StubDoc):
        self._doc = doc_stub

    def document(self, _project_id: str):
        return self._doc


class _StubDB:
    def __init__(self, doc_stub: _StubDoc):
        self._projects = _StubProjects(doc_stub)

    def collection(self, name: str):
        assert name == app_module.db.COLLECTION
        return self._projects


@pytest.fixture
def patched_db(monkeypatch):
    """Swap db.get_db() with a stub that yields configurable milestones."""
    state: dict = {"snaps": []}

    def _install(snaps: list[dict]) -> None:
        state["snaps"] = [_StubSnap(s) for s in snaps]

    def _get_db_stub():
        return _StubDB(_StubDoc(state["snaps"]))

    monkeypatch.setattr(app_module.db, "get_db", _get_db_stub)
    yield _install


def test_hydrate_v2_milestones_reads_subcollection(patched_db):
    """v2 (schema_version=2) data gets milestones re-attached from subcollection."""
    patched_db([
        {"id": "ms-1", "title": "first", "entries": []},
        {"id": "ms-2", "title": "second", "entries": []},
    ])
    meta_only = {
        "name": "proj",
        "schema_version": 2,
        "summary": "updated summary",
        # milestones intentionally absent — that's the v2 parent-doc shape
    }
    out = app_module._hydrate_v2_milestones("proj-abc", meta_only)

    assert [m["id"] for m in out["milestones"]] == ["ms-1", "ms-2"]
    assert out["summary"] == "updated summary"  # other fields preserved
    assert out["schema_version"] == 2


def test_hydrate_v1_passthrough_unchanged(patched_db):
    """v1 (no schema_version) data passes through without hitting Firestore."""
    patched_db([
        # Sentinel: if hydration runs anyway, we'd see these in the output.
        {"id": "should-not-appear", "title": "x", "entries": []},
    ])
    v1_data = {
        "name": "legacy",
        "milestones": [{"id": "ms-existing", "title": "kept", "entries": []}],
    }
    out = app_module._hydrate_v2_milestones("proj-legacy", v1_data)

    # Identity preservation: same dict reference is the contract for the v1
    # fast path (no copy needed when we don't touch anything).
    assert out is v1_data
    assert [m["id"] for m in out["milestones"]] == ["ms-existing"]


def test_hydrate_failure_falls_back_to_original_data(monkeypatch):
    """Firestore read failure must not drop the broadcast — return data unchanged."""
    def _exploding_get_db():
        raise RuntimeError("firestore is down")

    monkeypatch.setattr(app_module.db, "get_db", _exploding_get_db)
    v2_data = {
        "name": "proj",
        "schema_version": 2,
        "summary": "still need to broadcast this",
    }
    out = app_module._hydrate_v2_milestones("proj-x", v2_data)

    # Stale-but-present beats empty; broadcast continues without milestones key.
    assert out is v2_data
    assert "milestones" not in out
