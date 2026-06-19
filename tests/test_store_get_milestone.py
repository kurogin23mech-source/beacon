"""Tests for Store.get_milestone (ms-84 Phase 1).

Both LocalStore and StoreApi must return a milestone dict shaped like the
cloud API ``GET /milestones/{ms_id}`` response (= base milestone fields
plus ``total_tasks`` / ``done_tasks`` / JSON-serialised ``entries``). The
two backends share an error contract: ``ValueError`` when the milestone
is unknown, so CLI sites don't have to branch on backend type.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from store_local import LocalStore
from store_api import StoreApi


PROJECT_FIXTURE = {
    "name": "Fixture",
    "summary": "",
    "milestones": [
        {
            "id": "ms-1",
            "title": "first",
            "status": "in_progress",
            "progress": 50,
            "target_date": "",
            "entries": [
                {
                    "id": "e-100",
                    "type": "task",
                    "description": "alpha",
                    "status": "done",
                    "date": "2026-06-19",
                    "created_at": "2026-06-19T00:00:00",
                },
                {
                    "id": "e-101",
                    "type": "task",
                    "description": "beta",
                    "status": "todo",
                    "date": "2026-06-19",
                    "created_at": "2026-06-19T00:00:00",
                },
            ],
        },
        {
            "id": "ms-2",
            "title": "second",
            "status": "todo",
            "progress": 0,
            "target_date": "",
            "entries": [],
        },
    ],
}


# ---------------------------------------------------------------------------
# LocalStore
# ---------------------------------------------------------------------------


def _make_local_store(data: dict) -> LocalStore:
    tmpdir = tempfile.mkdtemp(prefix="beacon-store-test-")
    beacon_dir = os.path.join(tmpdir, ".beacon")
    os.makedirs(beacon_dir, exist_ok=True)
    pf = os.path.join(beacon_dir, "project.json")
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return LocalStore(pf)


def test_local_get_milestone_returns_task_counts():
    store = _make_local_store(PROJECT_FIXTURE)
    ms = store.get_milestone("ms-1")
    assert ms["id"] == "ms-1"
    assert ms["title"] == "first"
    assert ms["total_tasks"] == 2
    assert ms["done_tasks"] == 1
    # entries_to_json keeps order + structure
    assert [e["id"] for e in ms["entries"]] == ["e-100", "e-101"]


def test_local_get_milestone_zero_tasks():
    store = _make_local_store(PROJECT_FIXTURE)
    ms = store.get_milestone("ms-2")
    assert ms["total_tasks"] == 0
    assert ms["done_tasks"] == 0
    assert ms["entries"] == []


def test_local_get_milestone_unknown_raises_value_error():
    store = _make_local_store(PROJECT_FIXTURE)
    with pytest.raises(ValueError, match="ms-404"):
        store.get_milestone("ms-404")


# ---------------------------------------------------------------------------
# StoreApi
# ---------------------------------------------------------------------------


class FakeClient:
    """Just enough of ApiClient for get_milestone wiring tests."""

    def __init__(self, milestones: dict, raise_404_for: set[str] | None = None):
        self._ms = milestones
        self._404 = raise_404_for or set()
        self.calls: list[tuple[str, str]] = []

    def get_milestone(self, project_id: str, ms_id: str) -> dict:
        self.calls.append((project_id, ms_id))
        if ms_id in self._404:
            raise RuntimeError("API error 404: Milestone not found")
        return self._ms[ms_id]


def _make_store_api(client: FakeClient) -> StoreApi:
    store = StoreApi.__new__(StoreApi)
    store._client = client
    store._project_id = "proj-1"
    return store


def test_cloud_get_milestone_passes_through_api_response():
    payload = {
        "id": "ms-1",
        "title": "first",
        "status": "in_progress",
        "total_tasks": 2,
        "done_tasks": 1,
        "entries": [{"id": "e-100", "type": "task"}],
    }
    client = FakeClient({"ms-1": payload})
    store = _make_store_api(client)
    out = store.get_milestone("ms-1")
    assert out == payload
    assert client.calls == [("proj-1", "ms-1")]


def test_cloud_get_milestone_404_becomes_value_error():
    client = FakeClient({}, raise_404_for={"ms-404"})
    store = _make_store_api(client)
    with pytest.raises(ValueError, match="ms-404"):
        store.get_milestone("ms-404")


def test_cloud_get_milestone_other_runtime_error_passes_through():
    class BoomClient:
        def get_milestone(self, project_id, ms_id):
            raise RuntimeError("API error 500: server exploded")

    store = _make_store_api(BoomClient())
    with pytest.raises(RuntimeError, match="500"):
        store.get_milestone("ms-1")
