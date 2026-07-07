"""Unit tests for StoreApi lost-update conflict guard (e-841).

The CLI mutates the whole project document and PUTs it back wholesale. If the
cloud changed between load and save, a blind PUT would clobber the concurrent
change. StoreApi.save_project() must detect that and raise ConflictError.
"""

import sys
import os
import copy

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from store_api import StoreApi, ConflictError


class FakeClient:
    """In-memory stand-in for ApiClient.

    Mimics the HTTP serialization boundary by deep-copying on every get/put so
    callers never share mutable references with the stored document.
    """

    def __init__(self, doc):
        self._doc = copy.deepcopy(doc)
        self.put_calls = []

    def get_project(self, project_id):
        return copy.deepcopy(self._doc)

    def put_project(self, project_id, data):
        self.put_calls.append(copy.deepcopy(data))
        self._doc = copy.deepcopy(data)
        return {"status": "ok"}


def _make_store(doc):
    s = StoreApi("http://test", "pid", "")
    s._client = FakeClient(doc)
    StoreApi._load_baseline.pop("pid", None)  # isolate class-level state
    return s


def test_save_after_clean_load_succeeds():
    """No concurrent change -> save goes through."""
    s = _make_store({"name": "p", "summary": "v0", "milestones": []})
    data = s.load_project()
    data["summary"] = "mine"
    s.save_project(data)
    assert len(s._client.put_calls) == 1
    assert s._client.put_calls[-1]["summary"] == "mine"


def test_save_detects_concurrent_change():
    """Cloud changed after load -> ConflictError, and our PUT is suppressed."""
    s = _make_store({"name": "p", "summary": "v0", "milestones": []})
    data = s.load_project()
    # Another writer updates the cloud doc after our load.
    s._client._doc = {"name": "p", "summary": "v1-by-other", "milestones": []}
    data["summary"] = "v0-mine"
    with pytest.raises(ConflictError):
        s.save_project(data)
    assert s._client.put_calls == []  # nothing clobbered


def test_save_without_prior_load_writes():
    """No baseline recorded (no load this invocation) -> no false positive."""
    s = _make_store({"name": "p", "milestones": []})
    StoreApi._load_baseline.pop("pid", None)
    s.save_project({"name": "p", "summary": "x", "milestones": []})
    assert len(s._client.put_calls) == 1


def test_baseline_refreshes_after_save():
    """A successful save updates the baseline so a follow-up save still works."""
    s = _make_store({"name": "p", "summary": "v0", "milestones": []})
    data = s.load_project()
    data["summary"] = "first"
    s.save_project(data)
    data["summary"] = "second"
    s.save_project(data)  # must not raise — baseline tracked our own write
    assert len(s._client.put_calls) == 2
    assert s._client.put_calls[-1]["summary"] == "second"


# ---------------------------------------------------------------------------
# ms-95 / e-2710 — cloud round-trip MUST preserve bus_auto_execute_channels
# in the local mirror.
#
# Failure mode this guards against:
# 1. trek auto-arm writes 4 channels to local + cloud.
# 2. A later `load_project()` fetches the cloud doc (still has the field)
#    and _write_local_cache overwrites the local file.
# 3. If the field is silently dropped at any layer (= API serializer,
#    validation, write-back coercion), the local file ends up without it —
#    the inbox hook then reads [] and downgrades every trek event.
#
# This is the structural symmetry assertion that backs the
# `test_client_and_server_*` invariant tests in
# tests/test_bus_auto_execute_allowlist.py — they pin the **two readers**
# while this one pins the **write-back path** that feeds those readers.
# ---------------------------------------------------------------------------

def test_load_project_local_mirror_preserves_allowlist(tmp_path):
    """A load that triggers _write_local_cache MUST persist
    bus_auto_execute_channels into the local file. Without it, a cloud-side
    save of allowlist updates by one CLI invocation would be erased by the
    next `load_project` from a separate invocation."""
    import json as _json
    cache_path = tmp_path / "project.json"
    allowlist = [
        "trek-progress-check", "trek-trigger",
        "trek-task-review", "trek-leader-digest",
    ]
    cloud_doc = {
        "name": "p", "summary": "", "milestones": [],
        "bus_auto_execute_channels": allowlist,
    }
    s = _make_store(cloud_doc)
    s._local_cache_path = str(cache_path)

    s.load_project()

    assert cache_path.exists(), (
        "_write_local_cache must produce a local file after load_project")
    local = _json.loads(cache_path.read_text())
    assert local.get("bus_auto_execute_channels") == allowlist, (
        f"local mirror lost the allowlist round-tripping through StoreApi: "
        f"local={local.get('bus_auto_execute_channels')!r}. "
        f"This is the e-2710 failure surface — if this regresses, every "
        f"non-trek CLI invocation will silently erase the trek auto-arm "
        f"and the inbox hook will downgrade every trek event.")


def test_save_project_local_mirror_preserves_allowlist(tmp_path):
    """Symmetric: a save that triggers _write_local_cache MUST persist
    bus_auto_execute_channels into the local file. This is what
    `cmd_bus_auto_execute_add` and `_arm_trek_channels_and_budget` rely on
    to keep the inbox hook in sync after a CLI write."""
    import json as _json
    cache_path = tmp_path / "project.json"
    allowlist = ["trek-progress-check"]
    cloud_doc = {"name": "p", "summary": "", "milestones": []}
    s = _make_store(cloud_doc)
    s._local_cache_path = str(cache_path)

    # Mirror the real CLI flow: load, mutate, save.
    data = s.load_project()
    data["bus_auto_execute_channels"] = allowlist
    s.save_project(data)

    assert cache_path.exists()
    local = _json.loads(cache_path.read_text())
    assert local.get("bus_auto_execute_channels") == allowlist
