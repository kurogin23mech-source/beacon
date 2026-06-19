"""Tests for Store.get_trek (ms-84 Phase 2 / e-2036).

Both LocalStore and StoreApi must return a trek dict with the same shape
(= members / scope / halt / leader_session_id / status 等)。 Error contract:
``ValueError`` when the trek is unknown so CLI sites don't have to branch
on backend. Other RuntimeError (= 403 / 500 / transport) propagates as-is
so cmd_trek_show can preserve the legacy cloud-branch error message.
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


TREK_FIXTURE = {
    "trek_id": "tk-test-001",
    "title": "fixture trek",
    "status": "active",
    "type": "persistent",
    "leader_session_id": "sv-leader",
    "scope": [
        {"project": "p-1", "milestone": "ms-1"},
    ],
    "members": [
        {"email": "x@example.com", "role": "leader", "user_id": "u-1"},
    ],
    "halt": None,
    "created_at": "2026-06-19T08:00:00Z",
}


# ---------------------------------------------------------------------------
# LocalStore
# ---------------------------------------------------------------------------


def _make_local_store(treks: dict, monkeypatch) -> LocalStore:
    """Wire LocalStore + a temp trek_store dir holding the given treks."""
    tmpdir = tempfile.mkdtemp(prefix="beacon-store-trek-test-")
    beacon_dir = os.path.join(tmpdir, ".beacon")
    os.makedirs(beacon_dir, exist_ok=True)
    pf = os.path.join(beacon_dir, "project.json")
    with open(pf, "w", encoding="utf-8") as f:
        json.dump({"name": "fixture", "milestones": []}, f)

    # trek_store.get_trek_dir() reads BEACON_TREKS_DIR / falls back to
    # ~/.beacon/treks. Redirect to the test tmpdir so the fixture treks
    # live in isolation from any real ~/.beacon/ on the test box.
    trek_dir = os.path.join(tmpdir, "treks")
    os.makedirs(trek_dir, exist_ok=True)
    monkeypatch.setenv("BEACON_TREKS_DIR", trek_dir)

    for trek_id, doc in treks.items():
        with open(os.path.join(trek_dir, f"{trek_id}.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)

    return LocalStore(pf)


def test_local_get_trek_returns_full_doc(monkeypatch):
    store = _make_local_store({"tk-test-001": TREK_FIXTURE}, monkeypatch)
    t = store.get_trek("tk-test-001")
    assert t["trek_id"] == "tk-test-001"
    assert t["title"] == "fixture trek"
    assert t["status"] == "active"
    assert t["leader_session_id"] == "sv-leader"
    assert t["members"][0]["email"] == "x@example.com"


def test_local_get_trek_unknown_raises_value_error(monkeypatch):
    store = _make_local_store({}, monkeypatch)
    with pytest.raises(ValueError, match="tk-missing"):
        store.get_trek("tk-missing")


# ---------------------------------------------------------------------------
# StoreApi
# ---------------------------------------------------------------------------


class FakeClient:
    """Just enough of ApiClient for get_trek wiring tests."""

    def __init__(self, treks: dict, raise_404_for: set[str] | None = None):
        self._treks = treks
        self._404 = raise_404_for or set()
        self.calls: list[str] = []

    def get_trek(self, trek_id: str) -> dict:
        self.calls.append(trek_id)
        if trek_id in self._404:
            raise RuntimeError("API error 404: trek not found")
        return self._treks[trek_id]


def _make_store_api(client: FakeClient) -> StoreApi:
    store = StoreApi.__new__(StoreApi)
    store._client = client
    store._project_id = "proj-1"
    return store


def test_cloud_get_trek_passes_through_api_response():
    client = FakeClient({"tk-test-001": TREK_FIXTURE})
    store = _make_store_api(client)
    t = store.get_trek("tk-test-001")
    assert t == TREK_FIXTURE
    assert client.calls == ["tk-test-001"]


def test_cloud_get_trek_404_becomes_value_error():
    client = FakeClient({}, raise_404_for={"tk-missing"})
    store = _make_store_api(client)
    with pytest.raises(ValueError, match="tk-missing"):
        store.get_trek("tk-missing")


def test_cloud_get_trek_other_runtime_error_passes_through():
    class BoomClient:
        def get_trek(self, trek_id):
            raise RuntimeError("API error 500: server exploded")

    store = _make_store_api(BoomClient())
    with pytest.raises(RuntimeError, match="500"):
        store.get_trek("tk-test-001")
