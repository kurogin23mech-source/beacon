"""Cloud-mode CLI write-back to local .beacon/project.json (ms-95 / e-746).

Background
----------
Before this change, cloud-mode CLI calls only spoke to the cloud API and
never refreshed the on-disk ``.beacon/project.json``. The orphan local
cache stayed pinned to the ms-22-era snapshot that was uploaded at
cloud-join time (= 5/17 in the original observation). Tauri's
``load_project_json`` reads that file at startup and renders an outdated
world for the first few seconds until the WS push arrives (e-723).

ms-36 originally framed "cloud mode はローカル .beacon を読み取り専用
キャッシュ" but the write-back leg of that contract was never wired
through to StoreApi. ms-84 Phase 3 (e-2037) renamed the stale file aside
on cloud-join, which closed the silent-drift loop but did not fix the
Tauri startup render. This module pins the new contract: every successful
cloud read OR write also mirrors the canonical document back into
``.beacon/project.json``, so the cache is at worst one CLI invocation
behind cloud truth (= AC #1, #3, #4).

Direction is cloud→local only — the local file is never PUT back to the
cloud, preserving the ms-84 Phase 3 invariant that the cloud document is
the single source of truth for writes.
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from store_api import StoreApi


class FakeClient:
    """In-memory stand-in for ApiClient.

    Mimics the HTTP serialization boundary by deep-copying on every get/put
    so callers never share mutable references with the stored document.
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


def _reset_baseline():
    """Isolate the class-level _load_baseline between tests."""
    StoreApi._load_baseline.pop("pid", None)


def _make_store(doc, cache_path):
    s = StoreApi(
        "http://test", "pid", "",
        local_cache_path=str(cache_path),
    )
    s._client = FakeClient(doc)
    _reset_baseline()
    return s


def test_load_writes_cloud_response_to_local_cache(tmp_path):
    """AC #1 + #3 — load_project() mirrors the API response to disk so
    Tauri's load_project_json no longer sees a stale snapshot."""
    cache = tmp_path / ".beacon" / "project.json"
    fresh_doc = {
        "name": "p", "summary": "ms-46-era",
        "milestones": [{"id": "ms-46", "title": "latest"}],
    }
    s = _make_store(fresh_doc, cache)

    # Pre-condition: stale cache file present (= ms-22-era snapshot).
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"name": "p", "summary": "ms-22-era",
                                 "milestones": []}), encoding="utf-8")
    assert "ms-22-era" in cache.read_text(encoding="utf-8")

    returned = s.load_project()

    # The returned data is the cloud truth.
    assert returned["summary"] == "ms-46-era"
    # And the local cache has been refreshed with the cloud truth.
    assert cache.exists()
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["summary"] == "ms-46-era"
    assert written["milestones"][0]["id"] == "ms-46"


def test_save_writes_cloud_response_to_local_cache(tmp_path):
    """A successful cloud PUT also refreshes the on-disk cache so the next
    Tauri startup never sees the pre-save snapshot."""
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(
        {"name": "p", "summary": "v0", "milestones": []},
        cache,
    )

    data = s.load_project()
    data["summary"] = "v1-after-save"
    s.save_project(data)

    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["summary"] == "v1-after-save"


def test_load_regenerates_missing_cache_directory(tmp_path):
    """AC #4 — when no .beacon/project.json exists (= ms-84 Phase 3 renamed
    it aside), the first cloud-mode CLI call regenerates it in current
    v1 schema form so Tauri can resume reading from this path."""
    cache = tmp_path / "new-beacon-dir" / "project.json"
    assert not cache.exists()
    assert not cache.parent.exists()

    doc = {"name": "p", "summary": "regenerated",
           "milestones": [{"id": "ms-1"}]}
    s = _make_store(doc, cache)

    s.load_project()

    assert cache.exists(), "load_project should create the cache file"
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written == doc


def test_cache_write_is_atomic_via_temp_file(tmp_path):
    """The cache must be written atomically — a crash between two writes
    must never leave a half-truncated project.json that Tauri would then
    fail to parse on startup."""
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(
        {"name": "p", "summary": "v0", "milestones": []},
        cache,
    )

    s.load_project()

    # No leftover .tmp file once the write completes successfully.
    tmp_files = list(cache.parent.glob("*.tmp"))
    assert tmp_files == []
    # And the cache itself is valid JSON.
    json.loads(cache.read_text(encoding="utf-8"))


def test_no_cache_path_means_no_write(tmp_path, monkeypatch):
    """When the caller does not pass local_cache_path (= legacy test
    constructions, or contexts where there is no project file), the
    StoreApi must not invent a path to write to."""
    s = StoreApi("http://test", "pid", "")  # no local_cache_path
    s._client = FakeClient({"name": "p", "summary": "v", "milestones": []})
    _reset_baseline()
    # Chdir so a stray "" or "." path can't accidentally write into the
    # test workspace.
    monkeypatch.chdir(tmp_path)

    s.load_project()
    s.save_project({"name": "p", "summary": "v2", "milestones": []})

    # Nothing should have been created in the cwd.
    assert list(tmp_path.iterdir()) == []


def test_cache_write_failure_does_not_break_load(tmp_path, monkeypatch):
    """Best-effort contract: when the cache path is unwritable (= read-only
    mount, missing parent perms), the load must still succeed and return
    the cloud truth. The CLI command itself must not crash."""
    cache = tmp_path / ".beacon" / "project.json"
    fresh_doc = {"name": "p", "summary": "fresh", "milestones": []}
    s = _make_store(fresh_doc, cache)

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    # Patch the open() used inside the helper so the disk write fails.
    import builtins
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open",
        lambda *a, **kw: (boom() if str(a[0]).endswith(".tmp") else real_open(*a, **kw)),
    )

    # Should not raise.
    result = s.load_project()
    assert result["summary"] == "fresh"


def test_save_writes_after_conflict_recovery(tmp_path):
    """After a clean save-load-save cycle, the cache reflects the final
    state (= no skew between cloud and disk across multiple operations)."""
    cache = tmp_path / ".beacon" / "project.json"
    s = _make_store(
        {"name": "p", "summary": "v0", "milestones": []},
        cache,
    )

    d1 = s.load_project()
    d1["summary"] = "v1"
    s.save_project(d1)
    d2 = s.load_project()
    d2["summary"] = "v2"
    s.save_project(d2)

    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["summary"] == "v2"


# ---------------------------------------------------------------------------
# Integration: get_store() wires local_cache_path through automatically.
# ---------------------------------------------------------------------------


def test_get_store_passes_local_cache_path(tmp_path, monkeypatch):
    """End-to-end wiring: a real get_store() call in cloud mode must
    construct a StoreApi whose local_cache_path equals the project_file
    path, so the AC #1 write-back actually engages from the CLI entry
    point (not just in unit-test fixtures)."""
    proj = tmp_path / "proj"
    beacon_dir = proj / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"project_id": "pid-from-cloud", "api_url": "https://x"})
    )
    monkeypatch.setenv(
        "BEACON_PROJECT_FILE",
        str(beacon_dir / "project.json"),
    )
    # Avoid touching the real auth resolver during the test.
    import store as store_mod

    store_obj = store_mod.get_store()

    assert isinstance(store_obj, StoreApi)
    assert store_obj._local_cache_path == str(beacon_dir / "project.json")
