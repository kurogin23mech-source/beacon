"""Tests for Store.purge_milestone (ms-84 Phase 2).

LocalStore and StoreApi must expose a single purge_milestone API so
cmd_milestone_purge can drop its ``_is_cloud_mode()`` branch (= the
受入条件 10 target for ms-84). The two backends share an error contract
(ValueError on invalid input) and a return shape (``{purged,
still_dirty, dup_report}``), so the CLI does not have to know which
backend it is talking to.
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


# ---------------------------------------------------------------------------
# LocalStore
# ---------------------------------------------------------------------------


def _make_local_store(data: dict) -> LocalStore:
    tmpdir = tempfile.mkdtemp(prefix="beacon-purge-test-")
    beacon_dir = os.path.join(tmpdir, ".beacon")
    os.makedirs(beacon_dir, exist_ok=True)
    pf = os.path.join(beacon_dir, "project.json")
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return LocalStore(pf)


def _clean_project() -> dict:
    return {
        "name": "Fixture",
        "summary": "",
        "milestones": [
            {"id": "ms-1", "title": "first", "status": "todo",
             "progress": 0, "target_date": "", "entries": []},
            {"id": "ms-2", "title": "second", "status": "todo",
             "progress": 0, "target_date": "", "entries": []},
        ],
    }


def _dup_project(count: int = 2) -> dict:
    return {
        "name": "Fixture",
        "summary": "",
        "milestones": [
            {"id": "ms-9", "title": f"copy-{i}", "status": "todo",
             "progress": 0, "target_date": "", "entries": []}
            for i in range(1, count + 1)
        ],
    }


def test_local_purge_removes_record_and_persists():
    store = _make_local_store(_clean_project())
    out = store.purge_milestone("ms-1", reason="cleanup")
    assert out["purged"]["id"] == "ms-1"
    assert out["still_dirty"] is False
    # find_duplicate_ids returns three category buckets even when clean,
    # each empty — so we look at the values rather than equality on {}
    assert not any(out["dup_report"].values())
    reloaded = store.load_project()
    assert [m["id"] for m in reloaded["milestones"]] == ["ms-2"]


def test_local_purge_requires_index_for_duplicates():
    store = _make_local_store(_dup_project(count=2))
    with pytest.raises(ValueError, match="2 records"):
        store.purge_milestone("ms-9", reason="dup recovery")


def test_local_purge_with_index_keeps_remaining_records():
    """Two copies of ms-9 → purging one leaves a single, non-duplicate copy."""
    store = _make_local_store(_dup_project(count=2))
    out = store.purge_milestone("ms-9", reason="dup recovery", index=1)
    assert out["purged"]["title"] == "copy-1"
    # Only one ms-9 remains → no duplicate left, so still_dirty=False
    assert out["still_dirty"] is False
    reloaded = store.load_project()
    assert [m["title"] for m in reloaded["milestones"]] == ["copy-2"]


def test_local_purge_with_residual_duplicates_reports_still_dirty():
    """Three copies of ms-9 → after one purge, two duplicates still remain."""
    store = _make_local_store(_dup_project(count=3))
    out = store.purge_milestone("ms-9", reason="dup recovery", index=1)
    assert out["still_dirty"] is True
    assert out["dup_report"]["milestones"].get("ms-9") == 2
    # save_project bypasses validate_project, so the file persists the
    # still-dirty state (= the operator can purge the next one).
    reloaded = store.load_project()
    assert [m["title"] for m in reloaded["milestones"]] == ["copy-2", "copy-3"]


def test_local_purge_unknown_milestone_raises():
    store = _make_local_store(_clean_project())
    with pytest.raises(ValueError, match="ms-404"):
        store.purge_milestone("ms-404", reason="cleanup")


def test_local_purge_requires_reason():
    store = _make_local_store(_clean_project())
    with pytest.raises(ValueError, match="reason"):
        store.purge_milestone("ms-1", reason="")


# ---------------------------------------------------------------------------
# StoreApi
# ---------------------------------------------------------------------------


class FakeClient:
    """In-memory ApiClient stand-in for StoreApi mutation tests."""

    def __init__(self, *, response: dict | None = None,
                 raise_msg: str | None = None):
        self._response = response or {"id": "ms-1", "title": "stub",
                                      "purged": True}
        self._raise_msg = raise_msg
        self.calls: list[tuple] = []

    def purge_milestone(self, project_id: str, ms_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        self.calls.append((project_id, ms_id, reason, index))
        if self._raise_msg is not None:
            raise RuntimeError(self._raise_msg)
        return self._response


def _make_store_api(client: FakeClient) -> StoreApi:
    store = StoreApi.__new__(StoreApi)
    store._client = client
    store._project_id = "proj-1"
    return store


def test_cloud_purge_passes_args_through_and_returns_clean():
    fake = FakeClient(response={"id": "ms-1", "title": "first",
                                "purged": True})
    store = _make_store_api(fake)
    out = store.purge_milestone("ms-1", reason="cleanup", index=None)
    assert fake.calls == [("proj-1", "ms-1", "cleanup", None)]
    assert out["purged"]["id"] == "ms-1"
    assert out["still_dirty"] is False
    assert out["dup_report"] == {}


def test_cloud_purge_404_becomes_value_error():
    fake = FakeClient(raise_msg="API error 404: Milestone not found")
    store = _make_store_api(fake)
    with pytest.raises(ValueError, match="ms-404"):
        store.purge_milestone("ms-404", reason="cleanup")


def test_cloud_purge_403_becomes_value_error():
    fake = FakeClient(raise_msg="API error 403: owner only")
    store = _make_store_api(fake)
    with pytest.raises(ValueError, match="owner"):
        store.purge_milestone("ms-1", reason="cleanup")


def test_cloud_purge_other_runtime_error_passes_through():
    fake = FakeClient(raise_msg="API error 500: server exploded")
    store = _make_store_api(fake)
    with pytest.raises(RuntimeError, match="500"):
        store.purge_milestone("ms-1", reason="cleanup")


# ---------------------------------------------------------------------------
# Store.purge_entry — same contract, different backing call
# ---------------------------------------------------------------------------


def _entry_project() -> dict:
    return {
        "name": "Fixture",
        "summary": "",
        "milestones": [
            {"id": "ms-1", "title": "first", "status": "in_progress",
             "progress": 0, "target_date": "", "entries": [
                 {"id": "e-100", "type": "task", "description": "alpha",
                  "status": "todo", "date": "2026-06-19",
                  "created_at": "2026-06-19T00:00:00"},
                 {"id": "e-101", "type": "task", "description": "beta",
                  "status": "todo", "date": "2026-06-19",
                  "created_at": "2026-06-19T00:00:00"},
             ]},
        ],
    }


def test_local_purge_entry_removes_record():
    store = _make_local_store(_entry_project())
    out = store.purge_entry("e-100", reason="cleanup")
    assert out["purged"]["id"] == "e-100"
    assert out["still_dirty"] is False
    reloaded = store.load_project()
    remaining = [e["id"] for e in reloaded["milestones"][0]["entries"]]
    assert remaining == ["e-101"]


def test_local_purge_entry_unknown_raises():
    store = _make_local_store(_entry_project())
    with pytest.raises(ValueError, match="e-404"):
        store.purge_entry("e-404", reason="cleanup")


class FakeEntryClient:
    def __init__(self, *, response: dict | None = None,
                 raise_msg: str | None = None):
        self._response = response or {"entry_id": "e-1", "description": "stub",
                                      "purged": True}
        self._raise_msg = raise_msg
        self.calls: list[tuple] = []

    def purge_entry(self, project_id: str, entry_id: str, *,
                    reason: str, index: int | None = None) -> dict:
        self.calls.append((project_id, entry_id, reason, index))
        if self._raise_msg:
            raise RuntimeError(self._raise_msg)
        return self._response


def test_cloud_purge_entry_passes_args_through():
    fake = FakeEntryClient()
    store = StoreApi.__new__(StoreApi)
    store._client = fake
    store._project_id = "proj-1"
    out = store.purge_entry("e-1", reason="cleanup")
    assert fake.calls == [("proj-1", "e-1", "cleanup", None)]
    assert out["still_dirty"] is False
    assert out["dup_report"] == {}


def test_cloud_purge_entry_403_becomes_value_error():
    fake = FakeEntryClient(raise_msg="API error 403: owner only")
    store = StoreApi.__new__(StoreApi)
    store._client = fake
    store._project_id = "proj-1"
    with pytest.raises(ValueError, match="owner"):
        store.purge_entry("e-1", reason="cleanup")
